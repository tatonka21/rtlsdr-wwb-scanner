import os
import datetime
import uuid
from StringIO import StringIO
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom

EPOCH = datetime.datetime(1970, 1, 1)

class BaseExporter(object):
    def __init__(self, **kwargs):
        self.spectrum = kwargs.get('spectrum')
        self.filename = kwargs.get('filename')
    def __call__(self):
        self.build_data()
        self.write_file()
    @classmethod
    def export_to_file(cls, **kwargs):
        filename = kwargs.get('filename')
        ext = os.path.splitext(filename)[1].strip('.').lower()
        def find_exporter(_cls):
            if getattr(_cls, '_extension', None) == ext:
                return _cls
            for _subcls in _cls.__subclasses__():
                r = find_exporter(_subcls)
                if r is not None:
                    return r
            return None
        cls = find_exporter(cls)
        fh = cls(**kwargs)
        fh()
        return fh
    @property
    def filename(self):
        return getattr(self, '_filename', None)
    @filename.setter
    def filename(self, value):
        if value is None:
            return
        self.set_filename(value)
    def set_filename(self, value):
        if value == self.filename:
            return
        self._filename = value
    def build_data(self):
        raise NotImplementedError('Method must be implemented by subclasses')
    def write_file(self):
        s = self.build_data()
        with open(self.filename, 'w') as f:
            f.write(s)

class CSVExporter(BaseExporter):
    _extension = 'csv'
    newline_chars = '\r\n'
    delimiter_char = ','
    def __init__(self, **kwargs):
        super(CSVExporter, self).__init__(**kwargs)
        self.frequency_format = kwargs.get('frequency_format')
    def set_filename(self, value):
        if os.path.splitext(value)[1] == '.CSV':
            value = '.'.join([os.path.splitext(value)[0], 'csv'])
        super(CSVExporter, self).set_filename(value)
    def build_data(self):
        newline_chars = self.newline_chars
        delim = self.delimiter_char
        frequency_format = self.frequency_format
        lines = []
        for sample in self.spectrum.iter_samples():
            if frequency_format is None:
                f = sample.formatted_frequency
            else:
                f = frequency_format % (sample.frequency)
            lines.append(delim.join([
                f,
                sample.formatted_dbFS
            ]))
        return newline_chars.join(lines)

class BaseWWBExporter(BaseExporter):
    def __init__(self, **kwargs):
        super(BaseWWBExporter, self).__init__(**kwargs)
        self.dt = kwargs.get('dt', datetime.datetime.utcnow())
    def set_filename(self, value):
        ext = self._extension
        if os.path.splitext(value)[1].lower() != '.%s' % (ext):
            value = '.'.join([os.path.splitext(value)[0], ext])
        super(BaseWWBExporter, self).set_filename(value)
    def build_attribs(self):
        dt = self.dt
        spectrum = self.spectrum
        d = dict(
            scan_data_source=dict(
                ver='0.0.0.1',
                id='{%s}' % (uuid.uuid4()),
                model='TODO',
                name=os.path.abspath(self.filename),
                date=dt.strftime('%a %b %d %Y'),
                time=dt.strftime('%H:%M:%S'),
                color='#00ff00',
            ),
            data_sets=dict(
                count='1',
                no_data_value='-140',
            ),
        )
        d['data_set'] = dict(
            index='0',
            freq_units='KHz',
            ampl_units='dBm',
            start_freq=str(min(spectrum.samples.keys()) * 1000),
            stop_freq=str(max(spectrum.samples.keys()) * 1000),
            step_freq=str(spectrum.step_size * 1000),
            res_bandwidth='TODO',
            scale_factor='1',
            date=d['scan_data_source']['date'],
            time=d['scan_data_source']['time'],
            date_time=str(int((dt - EPOCH).total_seconds())),
        )
        return d
    def build_data(self):
        attribs = self.attribs = self.build_attribs()
        root = self.root = ET.Element('scan_data_source', attribs['scan_data_source'])
        ET.SubElement(root, 'data_sets', attribs['data_sets'])
        tree = self.tree = ET.ElementTree(root)
        return tree
    def write_file(self):
        tree = self.build_data()
        fd = StringIO()
        tree.write(fd, encoding='UTF-8', xml_declaration=True)
        doc = minidom.parseString(fd.getvalue())
        fd.close()
        s = doc.toprettyxml(encoding='UTF-8')
        with open(self.filename, 'w') as f:
            f.write(s)


class WWBLegacyExporter(BaseWWBExporter):
    _extension = 'sbd'
    def build_data(self):
        tree = super(WWBLegacyExporter, self).build_data()
        root = tree.getroot()
        spectrum = self.spectrum
        attribs = self.attribs
        data_sets = root.find('data_sets')
        data_set = ET.SubElement(data_sets, 'data_set', attribs['data_set'])
        for sample in spectrum.iter_samples():
            v = ET.SubElement(data_set, 'v')
            v.text = sample.formatted_dbFS
        return tree

class WWBExporter(BaseWWBExporter):
    _extension = 'sdb2'
    def build_data(self):
        tree = super(WWBExporter, self).build_data()
        root = tree.getroot()
        spectrum = self.spectrum
        data_sets = root.find('data_sets')
        freq_set = ET.SubElement(data_sets, 'freq_set')
        data_set = ET.SubElement(data_sets, 'data_set', self.attribs['data_set'])
        for sample in spectrum.iter_samples():
            f = ET.SubElement(freq_set, 'f')
            f.text = str(int(sample.frequency * 1000))
            v = ET.SubElement(data_set, 'v')
            v.text = sample.formatted_dbFS
        return tree
