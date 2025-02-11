# -*- coding:utf-8 -*-

# Copyright xmuspeech (Author: Leo 2022-01-11)
# refering https://github.com/wenet-e2e/wenet/blob/main/wenet/dataset/processor.py


import logging
import random,math
import tarfile
from subprocess import PIPE, Popen
from urllib.parse import urlparse

import torch
import torchaudio
import torchaudio.compliance.kaldi as kaldi
import libs.support.kaldi_io as kaldi_io
from libs.support.utils import batch_pad_right,get_torchaudio_backend
from .speech_augment import SpeechAug
from .signal_processing import de_silence
from libs.egs.augmentation import *
from libs.egs.kaldi_features import InputSequenceNormalization
torchaudio_backend = get_torchaudio_backend()
torchaudio.set_audio_backend(torchaudio_backend)

AUDIO_FORMAT_SETS = set(['flac', 'mp3', 'm4a', 'ogg', 'opus', 'wav', 'wma'])


def url_opener(data):
    """ Give url or local file, return file descriptor
        Inplace operation.

        Args:
            data(Iterable[{eg-path,...}]): eg-path: url or local file.

        Returns:
            Iterable[{eg-path, stream}]
    """
    for sample in data:
        assert 'eg-path' in sample
        # TODO(Binbin Zhang): support HTTP
        url = sample['eg-path']
        try:
            pr = urlparse(url)
            # local file
            if pr.scheme == '' or pr.scheme == 'file':
                stream = open(url, 'rb')
            # network file, such as HTTP(HDFS/OSS/S3)/HTTPS/SCP
            else:
                cmd = f'curl -s -L {url}'
                process = Popen(cmd, shell=True, stdout=PIPE)
                sample.update(process=process)
                stream = process.stdout
            sample.update(stream=stream)
            yield sample
        except Exception as ex:
            logging.warning('Failed to open {}'.format(url))


def tar_file_and_group(data):
    """ Expand a stream of open tar files into a stream of tar file contents.
        And groups the file with same prefix

        Args:
            data: Iterable[{eg-path, stream}]

        Returns:
            Iterable[{key, wav, label, sample_rate,lens}]
    """
    for sample in data:
        assert 'stream' in sample
        stream = tarfile.open(fileobj=sample['stream'], mode="r|*")
        prev_prefix = None
        example = {}
        valid = True
        for tarinfo in stream:
            name = tarinfo.name
            pos = name.rfind('.')
            assert pos > 0
            prefix, postfix = name[:pos], name[pos + 1:]
            if prev_prefix is not None and prefix != prev_prefix:
                example['key'] = prev_prefix
                if valid:
                    yield example
                example = {}
                valid = True
            with stream.extractfile(tarinfo) as file_obj:
                try:
                    if postfix == 'txt':
                        label = file_obj.read().decode('utf8')
                        example['label'] = int(label)
                    elif postfix in AUDIO_FORMAT_SETS:
                        waveform, sample_rate = torchaudio.load(file_obj)
                        example['wav'] = waveform[:1,:]
                        example['sample_rate'] = sample_rate
                        example['lens'] = torch.ones(1)
                    else:
                        example[postfix] = file_obj.read()
                except Exception as ex:
                    valid = False
                    logging.warning('error to parse {}'.format(name))
            prev_prefix = prefix
        if prev_prefix is not None:
            example['key'] = prev_prefix
            yield example
        stream.close()
        if 'process' in sample:
            sample['process'].communicate()
        sample['stream'].close()


def parse_raw(data):
    """ 
        data: Iterable[{eg-id,wav-path,class-label,...}], dict has id/wav/label

        Returns:
            Iterable[{key, wav, label, sample_rate,lens}]
    """
    for sample in data:

        assert 'eg-id' in sample
        assert 'wav-path' in sample
        assert 'class-label' in sample
        key = sample['eg-id']
        wav_file = sample['wav-path']
        label = sample['class-label']
        try:
            if 'start-position' in sample:
                assert 'end-position' in sample
                start, stop = int(sample['start-position']), int(sample['end-position'])
                waveform, sample_rate = torchaudio.load(
                    filepath=wav_file,
                    num_frames=stop - start,
                    frame_offset=start)
            else:
                waveform, sample_rate = torchaudio.load(wav_file)
            waveform = waveform[:1,:]
            label = int(label)
            lens = torch.ones(1)
            example = dict(key=key,
                           label=label,
                           wav=waveform,
                           lens=lens,
                           sample_rate=sample_rate)
            yield example
        except Exception as ex:
            logging.warning('Failed to read {}'.format(key))

def de_sil(data,win_len=0.1,min_eng=50,retry_times=1,force_output=True):
    """ 
        data: Iterable[{key, wav, label, lens, sample_rate}]

        Returns:
            Iterable[{key, wav, label, sample_rate, lens}]
    """
    for sample in data:
        assert 'wav' in sample
        assert 'key' in sample
        assert 'lens' in sample
        assert 'sample_rate' in sample        
        waveform = sample['wav']
        sr = sample['sample_rate']
        duration_sample=int(sample['lens']*(sample['wav'].shape[1]))
        waveform = waveform[:,0:duration_sample]
        cache_wave,cache_len = de_silence(waveform,sr=sr,win_len=win_len,min_eng=min_eng)
        while retry_times and cache_len==0:
            min_eng/=2
            cache_wave,_ = de_silence(waveform,sr=sr,win_len=win_len,min_eng=min_eng/2)
            retry_times-=1
        if force_output and cache_len==0:
            cache_wave=waveform
        sample['lens'] = torch.ones(waveform.shape[0])
        sample['wav'] = cache_wave
        del waveform
        yield sample

def random_chunk(data,chunk_len=2.015):
    """ 
        data: Iterable[{key, wav, label, lens, sample_rate}]

        Returns:
            Iterable[{key, wav, label, sample_rate, lens}]
    """
    for sample in data:
        assert 'wav' in sample
        assert 'key' in sample
        assert 'lens' in sample
        assert 'sample_rate' in sample
        waveform = sample['wav'] 
        duration_sample=int(sample['lens']*(sample['wav'].shape[1]))
        snt_len_sample = int(chunk_len*sample['sample_rate'])
        if duration_sample > snt_len_sample:
            start = random.randint(0, duration_sample - snt_len_sample - 1)
            stop = start + snt_len_sample
            sample['wav'] = waveform[:,start:stop]
        else:
            repeat_num = math.ceil(snt_len_sample/duration_sample)
            sample['wav'] = waveform[:,:duration_sample].repeat(1,repeat_num)[:,:snt_len_sample]
        sample['lens'] = torch.ones(waveform.shape[0])
        yield sample




def offline_feat(data):
    """ Read feats from kaldi ark.
        data: Iterable[{eg-id,wav-path,class-label,...}]
        Returns:
            Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
    """
    for sample in data:

        assert 'eg-id' in sample
        assert 'ark-path' in sample
        assert 'class-label' in sample
        key = sample['eg-id']
        ark_path = sample['ark-path']
        label = int(sample['class-label'])
        try:

            if 'start-position' in sample:
                assert 'end-position' in sample
                chunk = [int(sample['start-position']),int(sample['end-position'])]
                feat = kaldi_io.read_mat(ark_path, chunk=chunk)
            else:
                feat = kaldi_io.read_vec_flt(ark_path)
            feat = np.require(feat, requirements=['O', 'W'])
            feats = [torch.from_numpy(feat)]
            max_len = feats[0].size(0)

            yield dict(keys=[key],feats=feats,label=label,max_len=max_len)

        except Exception as ex:
            logging.warning('Failed to read {}'.format(key))


def resample(data, resample_rate=16000):
    """ Resample data.
        Inplace operation.

        Args:
            data: Iterable[{key, wav, label, lens, sample_rate}]
            resample_rate: target resample rate

        Returns:
            Iterable[{key, wav, label, lens, sample_rate}]
    """
    for sample in data:
        assert 'sample_rate' in sample
        assert 'wav' in sample
        sample_rate = sample['sample_rate']
        waveform = sample['wav']
        if sample_rate != resample_rate:
            sample['sample_rate'] = resample_rate
            sample['wav'] = torchaudio.transforms.Resample(
                orig_freq=sample_rate, new_freq=resample_rate)(waveform)
        yield sample



class SpeechAugPipline(object):
    def __init__(self, speechaug={}, tail_speechaug={}):
        super().__init__()

        self.speechaug = SpeechAug(**speechaug)
        self.tail_speechaug = SpeechAug(**tail_speechaug)
        speechaug_n_concat=self.speechaug.get_num_concat()
        tail_speechaug_n_concat=self.tail_speechaug.get_num_concat()
        # The concat number of speech augment, which is used to modify target.
        self.concat_pip= (speechaug_n_concat,tail_speechaug_n_concat)

    def __call__(self, data):
        """ speechaug.
            Args:
                data: Iterable[{key, wav, label, lens, sample_rate}]
            Returns:
                Iterable[{key, wav, label, lens, sample_rate}]
        """
        for sample in data:
            assert 'wav' in sample
            assert 'key' in sample
            assert 'lens' in sample
            assert 'label' in sample

            waveforms = sample['wav']
            lens = sample['lens']
            try:

                waveforms, lens = self.speechaug(waveforms, lens)

                waveforms, lens = self.tail_speechaug(waveforms, lens)
                sample['wav'] = waveforms

                sample['lens'] = lens
                yield sample
            except Exception as ex:
                logging.warning('Failed to speech aug {}'.format(sample['key']))



class KaldiFeature(object):
    """ This class extract features as kaldi's compute-mfcc-feats.

    Arguments
    ---------
    feat_type: str (fbank or mfcc).
    feature_extraction_conf: dict
        The config according to the kaldi's feature config.
    """

    def __init__(self,feature_type='mfcc',kaldi_featset={},mean_var_conf={}):
        super().__init__()
        assert feature_type in ['mfcc','fbank']
        self.feat_type=feature_type

        self.kaldi_featset=kaldi_featset
        if self.feat_type=='mfcc':
            self.extract=kaldi.mfcc
        else:
            self.extract=kaldi.fbank
        if mean_var_conf is not None:
            self.mean_var=InputSequenceNormalization(**mean_var_conf)
        else:
            self.mean_var=torch.nn.Identity()

    def __call__(self,data):
        """ make features.
            Args:
                data: Iterable[{key, wav, label, lens, sample_rate}]
            Returns:
                Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
        """
        for sample in data:
            assert 'wav' in sample
            assert 'key' in sample
            assert 'label' in sample
            assert 'lens' in sample
            assert 'sample_rate' in sample

            self.kaldi_featset['sample_frequency'] = sample['sample_rate']
            lens = sample['lens']
            waveforms = sample['wav']
            waveforms = waveforms * (1 << 15)
            feats = []
            label = sample['label']
            keys=[]
            utt = sample['key']
            try:
                with torch.no_grad():
                    lens=lens*waveforms.shape[1]

                    for i,wav in enumerate(waveforms):

                        if len(wav.shape)==1:
                            # add channel
                            wav=wav.unsqueeze(0)
                        wav= wav[:,:lens[i].long()]
                        feat=self.extract(wav,**self.kaldi_featset)
                        if(torch.any((torch.isnan(feat)))):
                            logging.warning('Failed to make featrue for {}, aug version:{}'.format(sample['key'],i))
                            pass
                        feat = feat.detach()
                        feat=self.mean_var(feat)

                        key = sample['key']+'#{}'.format(i) if i>0 else sample['key']
                        feats.append(feat)

                        keys.append(key)
                    if len(feats)==0:
                        pass

                    max_len = max([feat.size(0) for feat in feats])
                    yield dict(utt=utt,keys=keys,feats=feats,label=label,max_len=max_len)
            except Exception as ex:
                logging.warning('Failed to make featrue {}'.format(sample['key']))


class SpecAugPipline(object):
    def __init__(self,aug=None,aug_params={}):
        super().__init__()
        self.specaug=get_augmentation(aug,aug_params)
    def __call__(self,data):
        """ make features.
            Args:
                data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
            Returns:
                Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
        """
        for sample in data:
            assert 'keys' in sample
            assert 'feats' in sample

            feats = sample['feats']
            feats=[(self.specaug(feat.T)).T for feat in feats]
            sample['feats'] = feats

            yield sample


# def speed_perturb(data, speeds=None):
#     """ Apply speed perturb to the data.
#         Inplace operation.

#         Args:
#             data: Iterable[{key, wav, label, sample_rate}]
#             speeds(List[float]): optional speed

#         Returns:
#             Iterable[{key, wav, label, sample_rate}]
#     """
#     if speeds is None:
#         speeds = [0.9, 1.0, 1.1]
#     for sample in data:
#         assert 'sample_rate' in sample
#         assert 'wav' in sample
#         sample_rate = sample['sample_rate']
#         waveform = sample['wav']
#         speed = random.choice(speeds)
#         if speed != 1.0:
#             wav, _ = torchaudio.sox_effects.apply_effects_tensor(
#                 waveform, sample_rate,
#                 [['speed', str(speed)], ['rate', str(sample_rate)]])
#             sample['wav'] = wav

#         yield sample



def shuffle(data, shuffle_size=10000):
    """ Local shuffle the data

        Args:
            data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
            shuffle_size: buffer size for shuffle

        Returns:
            Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
    """
    buf = []
    for sample in data:
        buf.append(sample)
        if len(buf) >= shuffle_size:
            random.shuffle(buf)
            for x in buf:
                yield x
            buf = []
    # The sample left over
    random.shuffle(buf)
    for x in buf:
        yield x


def sort(data, sort_size=500):
    """ Sort the data by feature length.
        Sort is used after shuffle and before batch, so we can group
        utts with similar lengths into a batch, and `sort_size` should
        be less than `shuffle_size`

        Args:
            data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
            sort_size: buffer size for sort

        Returns:
            data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
    """

    buf = []
    for sample in data:
        buf.append(sample)
        if len(buf) >= sort_size:
            buf.sort(key=lambda x: x['max_len'])
            for x in buf:
                yield x
            buf = []
    # The sample left over
    buf.sort(key=lambda x: x['max_len'])
    for x in buf:
        yield x


def static_batch(data, batch_size=16):
    """ Static batch the data by `batch_size`
        Args:
            data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
            batch_size: batch size
        Returns:
            Iterable[List[{utt:str, keys:list, label, feats:list, max_len:int}]]
    """
    buf = []
    for sample in data:
        assert 'feats' in sample
        buf.append(sample)
        if len(buf) >= batch_size:
            yield buf
            buf = []
    if len(buf) > 0:
        yield buf


def dynamic_batch(data, max_frames_in_batch=12000):
    """ Dynamic batch the data until the total frames in batch
        reach `max_frames_in_batch`
        Args:
            data: Iterable[{utt:str, keys:list, label, feats:list, max_len:int}]
            max_frames_in_batch: max_frames in one batch
        Returns:
            Iterable[List[{utt:str, keys:list, label, feats:list, max_len:int}]]
    """
    buf = []
    longest_frames = 0
    for sample in data:
        assert 'feats' in sample
        assert 'max_len' in sample
        assert 'keys' in sample
        new_max_sample_frames = sample['max_len']
        new_num = len(sample['keys'])
        longest_frames = max(longest_frames, new_max_sample_frames)
        frames_after_padding = longest_frames * (sum([len(x['keys']) for x in buf]) + new_num)
        if frames_after_padding > max_frames_in_batch:
            yield buf
            buf = [sample]
            longest_frames = new_max_sample_frames
        else:
            buf.append(sample)
    if len(buf) > 0:
        yield buf


def batch(data, batch_type='static', batch_size=16, max_frames_in_batch=12000):
    """ Wrapper for static/dynamic batch
    """
    if batch_type == 'static':
        return static_batch(data, batch_size)

    elif batch_type == 'dynamic':
        return dynamic_batch(data, max_frames_in_batch)
    else:
        logging.fatal('Unsupported batch type {}'.format(batch_type))
    


def padding(data):
    """ Padding the data into training data
        Args:
            data: Iterable[List[{utt:str, keys:list, label, feats:list, max_len:int}]]
        Returns:
            Iterable[Tuple(keys, feats, labels, feats lengths, label lengths)]
    """
    for sample in data:

        assert isinstance(sample, list)
        feats=[]
        labels=[]
        keys=[]
        for x in sample:
            feats.extend(x['feats'])

            labels.extend([x['label']]*len(x['feats']))
            keys.extend(x['keys'])

        labels = torch.LongTensor(labels)
        feats = [(x.T) for x in feats]
        padded_feats, lens = batch_pad_right(feats)

        yield (padded_feats, labels)

