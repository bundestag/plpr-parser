# coding: utf-8
from __future__ import print_function
import os
import re
import logging
import requests
import dataset
from lxml import html
from urllib.parse import urljoin
from normdatei.text import clean_text, clean_name, fingerprint, extract_agenda_numbers
from normdatei.parties import search_party_names
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

log = logging.getLogger(__name__)

DATA_PATH = os.environ.get('DATA_PATH', 'data')
TXT_DIR = os.path.join(DATA_PATH, 'txt')
OUT_DIR = os.path.join(DATA_PATH, 'out')

INDEX_URL = 'https://www.bundestag.de/plenarprotokolle'
ARCHIVE_URL = 'http://webarchiv.bundestag.de/archive/2013/0927/dokumente/protokolle/plenarprotokolle/plenarprotokolle/17%03.d.txt'

CHAIRS = ['Vizepräsidentin', 'Vizepräsident', 'Präsident', 'Präsidentin', 'Alterspräsident', 'Alterspräsidentin']

SPEAKER_STOPWORDS = ['ich zitiere', 'zitieren', 'Zitat', 'zitiert',
                     'ich rufe den', 'ich rufe die',
                     'wir kommen zur Frage', 'kommen wir zu Frage', 'bei Frage',
                     'fordert', 'fordern', u'Ich möchte',
                     'Darin steht', ' Aspekte ', ' Punkte ', 'Berichtszeitraum']

BEGIN_MARK = re.compile('Beginn: [X\d]{1,2}.\d{1,2} Uhr')
END_MARK = re.compile('(\(Schluss:.\d{1,2}.\d{1,2}.Uhr\).*|Schluss der Sitzung)')

# speaker types
PARTY_MEMBER = re.compile('\s*(.{5,140}\(.*\)):\s*$')
PRESIDENT = re.compile('\s*((Alterspräsident(?:in)?|Vizepräsident(?:in)?|Präsident(?:in)?).{5,140}):\s*$')
STAATSSEKR = re.compile('\s*(.{5,140}, Parl\. Staatssekretär.*):\s*$')
STAATSMINISTER = re.compile('\s*(.{5,140}, Staatsminister.*):\s*$')
MINISTER = re.compile('\s*(.{5,140}, Bundesminister.*):\s*$')

TOP_MARK = re.compile('.*(?: rufe.*der Tagesordnung|Tagesordnungspunkt|Zusatzpunkt)(.*)')
POI_MARK = re.compile('\((.*)\)\s*$', re.M)
WRITING_BEGIN = re.compile('.*werden die Reden zu Protokoll genommen.*')
WRITING_END = re.compile(u'(^Tagesordnungspunkt .*:\s*$|– Drucksache d{2}/\d{2,6} –.*|^Ich schließe die Aussprache.$)')

db = os.environ.get('DATABASE_URI', 'sqlite:///data.sqlite')
print("USING DATABASE {}".format(db))
eng = dataset.connect(db)
table = eng['de_bundestag_plpr']

engine = create_engine(db)
Base = declarative_base()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db_session = Session()


class Utterance(Base):
    __tablename__ = "de_bundestag_plpr"

    id = Column(Integer, primary_key=True)
    wahlperiode = Column(Integer)
    sitzung = Column(Integer)
    sequence = Column(Integer)
    speaker_cleaned = Column(String)
    speaker_party = Column(String)
    speaker = Column(String)
    speaker_fp = Column(String)
    in_writing = Column(Boolean)
    top = Column(String)
    type = Column(String)
    text = Column(String)

    @staticmethod
    def from_dict(dictionary):
        utterance = Utterance()
        for key, value in dictionary.items():
            setattr(utterance, key, value)
        return utterance


class SpeechParser(object):
    def __init__(self, lines):
        self.lines = lines
        self.tops = []
        self.was_chair = True

    def parse_pois(self, group):
        for poi in group.split(' - '):
            text = poi
            speaker_name = None
            sinfo = poi.split(': ', 1)
            if len(sinfo) > 1:
                speaker_name = sinfo[0]
                text = sinfo[1]
            yield (speaker_name, text)

    def __iter__(self):
        self.in_session = False
        self.chair = False
        self.text = []
        self.speaker = None

        def emit():
            data = {
                'speaker': self.speaker,
                'type': 'chair' if self.chair else 'speech',
                'text': "\n\n".join(self.text).strip(),
                'top': ", ".join(self.tops) if self.tops else None,
            }
            self.was_chair = self.chair
            self.text = []
            return data

        def emit_poi(speaker, text):
            self.was_chair = False
            return {
                'speaker': speaker,
                'type': 'poi',
                'top': ", ".join(self.tops) if self.tops else None,
                'text': text
            }

        for line in self.lines:
            rline = line.strip()

            if not self.in_session and BEGIN_MARK.match(line):
                self.in_session = True
                continue
            elif not self.in_session:
                continue

            if END_MARK.match(rline):
                return

            if not len(rline):
                continue

            is_top = False
            top_match = TOP_MARK.match(line)
            if self.chair and top_match:
                new_top = extract_agenda_numbers(top_match.group(1))
                if not self.was_chair and new_top:
                    self.tops = []
                if new_top:
                    self.tops += new_top
                is_top = True

            has_stopword = False
            for sw in SPEAKER_STOPWORDS:
                if sw.lower() in line.lower():
                    has_stopword = True

            speaker_match = (PRESIDENT.match(line) or
                             PARTY_MEMBER.match(line) or
                             STAATSSEKR.match(line) or
                             STAATSMINISTER.match(line) or
                             MINISTER.match(line))
            if speaker_match is not None \
                    and not is_top \
                    and not has_stopword:
                if self.speaker is not None and self.text:
                    yield emit()
                role = line.strip().split(' ')[0]
                self.speaker = speaker_match.group(1)
                self.chair = role in CHAIRS
                continue

            poi_match = POI_MARK.match(rline)
            if poi_match is not None:
                # if not poi_match.group(1).lower().strip().startswith('siehe'):
                yield emit()
                for speaker, text in self.parse_pois(poi_match.group(1)):
                    yield emit_poi(speaker, text)
                continue

            self.text.append(rline)
        yield emit()


def file_metadata(filename):
    fname = os.path.basename(filename)
    return int(fname[:2]), int(fname[2:5])


def parse_transcript(filename):
    wp, session = file_metadata(filename)
    try:
        with open(filename) as fh:
            content = fh.read()
            text = clean_text(content)
    except UnicodeDecodeError:
        print("Reloading in other encoding (windows-1252)")
        with open(filename, encoding="windows-1252") as fh:
            content = fh.read()
            text = clean_text(content)

    db_session.query(Utterance) \
              .filter(Utterance.wahlperiode == wp) \
              .filter(Utterance.sitzung == session) \
              .delete(synchronize_session=False)

    base_data = {
        'filename': filename,
        'sitzung': session,
        'wahlperiode': wp
    }
    print("Loading transcript: %s/%.3d, from %s" % (wp, session, filename))
    seq = 0
    parser = SpeechParser(text.split('\n'))

    entries = []
    for contrib in parser:
        contrib.update(base_data)
        contrib['sequence'] = seq
        contrib['speaker_cleaned'] = clean_name(contrib['speaker'])
        contrib['speaker_fp'] = fingerprint(contrib['speaker_cleaned'])
        contrib['speaker_party'] = search_party_names(contrib['speaker'])
        seq += 1
        entries.append(contrib)
    db_session.bulk_insert_mappings(Utterance, entries)
    db_session.commit()

    # q = '''SELECT * FROM de_bundestag_plpr WHERE wahlperiode = :w AND sitzung = :s
    #         ORDER BY sequence ASC'''
    # fcsv = os.path.basename(filename).replace('.txt', '.csv')
    # rp = eng.query(q, w=wp, s=session)
    # dataset.freeze(rp, filename=fcsv, prefix=OUT_DIR, format='csv')


def fetch_protokolle():
    for d in TXT_DIR, OUT_DIR:
        try:
            os.makedirs(d)
        except:
            pass

    urls = set()
    res = requests.get(INDEX_URL)
    doc = html.fromstring(res.content)
    for a in doc.findall('.//a'):
        url = urljoin(INDEX_URL, a.get('href'))
        if url.endswith('.txt'):
            urls.add(url)

    for i in range(30, 260):
        url = ARCHIVE_URL % i
        urls.add(url)

    new_urls = get_new_urls()
    urls = urls.union(new_urls)
    offset = 20
    while len(new_urls) == 20:
        new_urls = get_new_urls(offset)
        urls = urls.union(new_urls)
        offset += 20

    for url in urls:
        txt_file = os.path.join(TXT_DIR, os.path.basename(url))
        txt_file = txt_file.replace('-data', '')
        if os.path.exists(txt_file):
            continue

        r = requests.get(url)
        if r.status_code < 300:
            with open(txt_file, 'wb') as fh:
                fh.write(r.content)

            print(url, txt_file)


def get_new_urls(offset=0):
    base_url = "https://www.bundestag.de"
    url = "https://www.bundestag.de/ajax/filterlist/de/dokumente/protokolle/plenarprotokolle/plenarprotokolle/-/455046/"
    params = {"limit": 20,
              "noFilterSet": "true",
              "offset": offset
              }
    res = requests.get(url, params=params)
    doc = html.fromstring(res.content)
    return {urljoin(base_url, a.get('href')) for a in doc.findall('.//a')}


if __name__ == '__main__':
    fetch_protokolle()

    for filename in os.listdir(TXT_DIR):
        parse_transcript(os.path.join(TXT_DIR, filename))
