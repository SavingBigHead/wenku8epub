"""Conservative lexical alignment. Scores are similarities, not probabilities."""
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import math
import re
import unicodedata

from .locations import START, chapter_start, chapter_end


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).lower() if c.isalnum())


def grams(text):
    return Counter(text[i:i + 2] for i in range(max(0, len(text) - 1)))


class Matcher:
    def __init__(self, book):
        self.book = book
        self.texts = [normalize(p.text) for p in book.paragraphs]
        self.n = len(self.texts)
        self.windows = {}
        self.idf = {}
        self.inverted = {'before': defaultdict(list), 'after': defaultdict(list)}
        raw = []
        df = Counter()
        for boundary in range(self.n + 1):
            sides = {}
            for side in ['before', 'after']:
                variations = []
                for count in (1, 2, 3):
                    if side == 'before':
                        selected = self.texts[max(0, boundary - count):boundary]
                        value = ''.join(selected)[-300:]
                    else:
                        value = ''.join(self.texts[boundary:boundary + count])[:300]
                    if value and value not in variations:
                        variations.append(value)
                sides[side] = variations
                combined = grams(''.join(self.texts[max(0, boundary - 2):boundary])[-300:] if side == 'before'
                                 else ''.join(self.texts[boundary:boundary + 2])[:300])
                raw.append((side, boundary, combined))
                df.update(combined.keys())
            self.windows[boundary] = sides
        for token, frequency in df.items():
            self.idf[token] = math.log(1 + (2 * (self.n + 1)) / (1 + frequency))
        for side, boundary, counts in raw:
            vector = self.vector(counts)
            for token, weight in vector.items():
                self.inverted[side][token].append((boundary, weight))

    def vector(self, counts):
        values = {token: (1 + math.log(count)) * self.idf.get(token, 1.0) for token, count in counts.items()}
        norm = math.sqrt(sum(x * x for x in values.values())) or 1
        return {token: value / norm for token, value in values.items()}

    def retrieval(self, text, side):
        hits = defaultdict(float)
        for token, weight in self.vector(grams(text)).items():
            for boundary, other in self.inverted[side].get(token, ()):
                hits[boundary] += weight * other
        return hits

    def similarity(self, left, right):
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        a, b = self.vector(grams(left)), self.vector(grams(right))
        cosine = sum(weight * b.get(token, 0) for token, weight in a.items())
        sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
        return .62 * cosine + .38 * sequence

    def suggestions(self, image):
        before = normalize(''.join(image.before))[-300:]
        after = normalize(''.join(image.after))[:300]
        if not before and not after:
            return []
        left = self.retrieval(before, 'before') if before else {}
        right = self.retrieval(after, 'after') if after else {}
        hit_ids = set(left) | set(right)
        rough = {b: ((left.get(b, 0) + right.get(b, 0)) / (bool(before) + bool(after))) for b in hit_ids}
        candidates = set(sorted(rough, key=rough.get, reverse=True)[:36])
        # A strong match on either side must not be lost in the first-pass average.
        candidates.update(sorted(left, key=left.get, reverse=True)[:8])
        candidates.update(sorted(right, key=right.get, reverse=True)[:8])
        output = []
        for boundary in candidates:
            window = self.windows[boundary]
            a = max((self.similarity(before, s) for s in window['before']), default=0) if before else None
            b = max((self.similarity(after, s) for s in window['after']), default=0) if after else None
            available = [x for x in [a, b] if x is not None]
            score = .70 * (sum(available) / len(available)) + .30 * min(available)
            if len(available) == 1:
                score *= .85
            # Very short anchors ("嗯", "是的") cannot establish a reliable location.
            if len(before) + len(after) < 18:
                score *= .6
            output.append({
                'after_id': START if boundary == 0 else self.book.paragraphs[boundary - 1].id,
                'boundary': boundary, 'score': round(score, 4),
                'before_score': round(a, 4) if a is not None else None,
                'after_score': round(b, 4) if b is not None else None,
            })
        expanded = []
        for candidate in sorted(output,key=lambda x:x['score'],reverse=True)[:12]:
            boundary = candidate['boundary']
            if 0 < boundary < self.n and self.book.paragraphs[boundary-1].chapter_id != self.book.paragraphs[boundary].chapter_id:
                before = self.book.paragraphs[boundary-1]
                after = self.book.paragraphs[boundary]
                anchors = [(chapter_end(before.chapter_id),before.chapter_title),(chapter_start(after.chapter_id),after.chapter_title)]
                # Context cannot distinguish both sides of a chapter break.
                anchors.sort(key=lambda pair: self.similarity(normalize(image.chapter),normalize(pair[1])),reverse=True)
                expanded.extend({**candidate,'after_id':anchor,'chapter_edge':True} for anchor,_ in anchors)
            else:
                expanded.append(candidate)
        return expanded


def align(book, donor):
    bodies = [image for image in donor.illustrations if image.role == 'body']
    if not bodies:
        return
    matcher = Matcher(book)
    for image in bodies:
        image.suggestions = matcher.suggestions(image)
    # Beam dynamic programming preserves the donor's image order. Skipping an
    # ambiguous image is allowed, rather than forcing all later images off course.
    beam = [(0.0, 0, [])]
    for image in bodies:
        states = []
        for total, previous, path in beam:
            states.append((total, previous, path + [None]))
            for candidate in image.suggestions:
                if candidate['boundary'] >= previous and candidate['score'] >= .30:
                    tie = .003 * (1 - abs(candidate['boundary'] / max(1, matcher.n) - image.ordinal))
                    states.append((total + candidate['score'] - .30 + tie,
                                   candidate['boundary'], path + [candidate]))
        # Keep only the strongest history for each last boundary.
        best = {}
        for state in states:
            if state[1] not in best or state[0] > best[state[1]][0]:
                best[state[1]] = state
        beam = sorted(best.values(), key=lambda x: x[0], reverse=True)[:80]
    selected = beam[0][2]
    for image, proposal in zip(bodies, selected):
        if proposal:
            image.after_id, image.score = proposal['after_id'], proposal['score']
            image.note = '根据前后中文文本匹配；请核对后确认。'
            if proposal.get('chapter_edge'): image.note += '这里是章节交界，请区分上一章结尾与下一章开头。'
            if image.suggestions and proposal != image.suggestions[0]:
                image.note += '已参考插图顺序调整候选位置。'
        else:
            image.after_id = None
            image.score = image.suggestions[0]['score'] if image.suggestions else 0
            image.note = '未找到足够明确且顺序一致的位置，请手动指定或忽略。'
        image.confirmed = False
