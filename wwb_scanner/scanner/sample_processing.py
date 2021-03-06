import time
import threading
import numpy as np
from scipy.signal.windows import __all__ as WINDOW_TYPES
from scipy.signal import welch, get_window

from wwb_scanner.core import JSONMixin

WINDOW_TYPES = [s for s in WINDOW_TYPES if s != 'get_window']

def next_2_to_pow(val):
    val -= 1
    val |= val >> 1
    val |= val >> 2
    val |= val >> 4
    val |= val >> 8
    val |= val >> 16
    return val + 1

def calc_num_samples(num_samples):
    return next_2_to_pow(int(num_samples))

def sort_psd(f, Pxx, onesided=False):
    f_index = np.argsort(f)
    f = f[f_index]
    Pxx = Pxx[f_index]
    if onesided:
        i = np.searchsorted(f, 0)
        f = f[i:]
        Pxx = Pxx[i:]
        Pxx *= 2
    return f, Pxx

class SampleSet(JSONMixin):
    __slots__ = ('scanner', 'center_frequency', 'raw', 'current_sweep',
                 '_frequencies', 'powers', 'collection', 'process_thread')
    def __init__(self, **kwargs):
        for key in self.__slots__:
            if key == '_frequencies':
                key = 'frequencies'
            setattr(self, key, kwargs.get(key))
        if self.scanner is None and self.collection is not None:
            self.scanner = self.collection.scanner
    @property
    def frequencies(self):
        f = getattr(self, '_frequencies', None)
        if f is None:
            f = self._frequencies= self.calc_expected_freqs()
        return f
    @frequencies.setter
    def frequencies(self, value):
        self._frequencies = value
    @property
    def sweeps_per_scan(self):
        return self.scanner.sweeps_per_scan
    @property
    def samples_per_sweep(self):
        return self.scanner.samples_per_sweep
    def read_samples(self):
        scanner = self.scanner
        freq = self.center_frequency
        sweeps_per_scan = scanner.sweeps_per_scan
        samples_per_sweep = scanner.samples_per_sweep
        sdr = scanner.sdr
        sdr.set_center_freq(freq)
        self.raw = np.zeros((sweeps_per_scan, samples_per_sweep), 'complex')
        self.powers = np.zeros((sweeps_per_scan, samples_per_sweep), 'float64')
        sdr.read_samples_async(self.samples_callback, num_samples=samples_per_sweep)
    def samples_callback(self, iq, context):
        current_sweep = getattr(self, 'current_sweep', None)
        if current_sweep is None:
            current_sweep = self.current_sweep = 0
        if current_sweep >= self.raw.shape[0]:
            self.on_sample_read_complete()
            return
        try:
            self.raw[current_sweep] = iq
            self.process_sweep(current_sweep)
        except:
            self.on_sample_read_complete()
            raise
        self.current_sweep += 1
        if current_sweep > self.raw.shape[0]:
            self.on_sample_read_complete()
    def on_sample_read_complete(self):
        sdr = self.scanner.sdr
        if not sdr.read_async_canceling:
            sdr.cancel_read_async()
        self.process_samples()
    def launch_process_thread(self):
        self.process_thread = ProcessThread(self)
    def process_sweep(self, sweep):
        scanner = self.scanner
        freq = self.center_frequency
        f, powers = welch(self.raw[sweep], fs=scanner.sample_rate)
        f += freq
        f /= 1e6
        powers = 10. * np.log10(powers)
        self.collection.on_sweep_processed(sample_set=self,
                                           powers=powers,
                                           frequencies=f)
    def translate_freq(self, samples, freq):
        # Adapted from https://github.com/vsergeev/luaradio/blob/master/radio/blocks/signal/frequencytranslator.lua
        rs = self.scanner.sample_rate
        omega = 2 * np.pi * (freq / rs)
        def iter_phase():
            p = 0
            i = 0
            while i < samples.shape[-1]:
                yield p
                p += omega
                p -= 2 * np.pi
                i += 1
        phase_rot = np.fromiter(iter_phase(), dtype=np.float)
        phase_rot = np.unwrap(phase_rot)
        xlator = np.zeros(phase_rot.size, dtype=samples.dtype)
        xlator.real = np.cos(phase_rot)
        xlator.imag = np.sin(phase_rot)
        samples *= xlator
        return samples
    def process_samples(self):
        rs = self.scanner.sample_rate
        fc = self.center_frequency
        samples = self.raw.flatten()
        samples = self.translate_freq(samples, fc * -1)
        overlap_ratio = self.scanner.sampling_config.sweep_overlap_ratio
        nperseg = 256
        win = get_window('hann', nperseg)
        f, powers = welch(samples, fs=rs,
                          window=win, nperseg=nperseg, scaling='density')

        iPxx = np.fft.ifft(powers)
        iPxx = self.translate_freq(iPxx, fc)
        powers = np.fft.fft(iPxx)

        f, powers = sort_psd(f, powers)
        crop = int((f.size * overlap_ratio) / 2)
        f, powers = f[crop:crop*-1], powers[crop:crop*-1]
        f += fc
        f /= 1e6
        self.powers = powers
        if not np.array_equal(f, self.frequencies):
            print 'freq not equal: %s, %s' % (self.frequencies.size, f.size)
            self.frequencies = f
        self.collection.on_sample_set_processed(self)
    def calc_expected_freqs(self):
        freq = self.center_frequency
        scanner = self.scanner
        rs = scanner.sample_rate
        num_samples = scanner.samples_per_sweep * scanner.sweeps_per_scan
        overlap_ratio = scanner.sampling_config.sweep_overlap_ratio
        fake_samples = np.zeros(num_samples, 'complex')
        f_expected, Pxx = welch(fake_samples, fs=rs, nperseg=256)
        f_expected, Pxx = sort_psd(f_expected, Pxx)
        crop = int((f_expected.size * overlap_ratio) / 2)
        f_expected, Pxx = f_expected[crop:crop*-1], Pxx[crop:crop*-1]
        f_expected += freq
        f_expected /= 1e6
        return f_expected
    def _serialize(self):
        d = {}
        for key in self.__slots__:
            if key in ['scanner', 'collection']:
                continue
            val = getattr(self, key)
            d[key] = val
        return d


class SampleCollection(JSONMixin):
    def __init__(self, **kwargs):
        self.scanner = kwargs.get('scanner')
        self.scanning = threading.Event()
        self.stopped = threading.Event()
        self.sample_sets = {}
    def add_sample_set(self, sample_set):
        self.sample_sets[sample_set.center_frequency] = sample_set
    def build_sample_set(self, freq):
        sample_set = SampleSet(collection=self, center_frequency=freq)
        self.add_sample_set(sample_set)
        return sample_set
    def scan_freq(self, freq):
        self.build_process_pool()
        sample_set = self.sample_sets.get(freq)
        if sample_set is None:
            sample_set = self.build_sample_set(freq)
        sample_set.read_samples()
        return sample_set
    def scan_all_freqs(self):
        self.scanning.set()
        for key in sorted(self.sample_sets.keys()):
            if not self.scanning.is_set():
                break
            sample_set = self.sample_sets[key]
            sample_set.read_samples()
        self.scanning.clear()
        self.stopped.set()
    def stop(self):
        if self.scanning.is_set():
            self.scanning.clear()
            self.stopped.wait()
    def cancel(self):
        if self.scanning.is_set():
            self.scanning.clear()
            self.stopped.wait()
    def on_sweep_processed(self, **kwargs):
        self.scanner.on_sweep_processed(**kwargs)
    def on_sample_set_processed(self, sample_set):
        self.scanner.on_sample_set_processed(sample_set)
    def _serialize(self):
        return {'sample_sets':
            {k: v._serialize() for k, v in self.sample_sets.items()},
        }
    def _deserialize(self, **kwargs):
        for key, val in kwargs.get('sample_sets', {}).items():
            sample_set = SampleSet.from_json(val, collection=self)
            self.sample_sets[key] = sample_set
