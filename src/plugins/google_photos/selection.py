"""Photo selection for the Google Photos plugin (pure logic, no I/O).

Each draw:
1. picks the pool, "memories" or "all photos", so that over time the memories
   share matches the configured percentage. It falls back to all photos when
   there is no memory.
2. picks an album among those that have photos in that pool, so that over
   time each album gets its share (automatic = proportional to its number of
   photos, or the user's percentages).
3. takes the next photo of that album's pool from a shuffle bag, so that no
   photo repeats before the others of the pool were shown.

Steps 1 and 2 use smooth weighted round-robin, which is deterministic and
matches the target percentages even over a few draws, unlike random sampling.
"""

import random
from datetime import date, datetime, timezone

MEMORY_MONTH = "month"
MEMORY_DAY = "day"
ORIENTATIONS = ("any", "landscape", "portrait")


# ------------------------------------------------------------------ photo info

def photo_datetime(photo, tz):
    """Local capture datetime of a photo, or None when unknown."""
    taken = photo.get("taken")
    if not isinstance(taken, (int, float)) or taken <= 0:
        return None
    return datetime.fromtimestamp(taken / 1000, tz=timezone.utc).astimezone(tz)


def orientation(photo):
    width, height = photo.get("width") or 0, photo.get("height") or 0
    if not width or not height:
        return None
    if width > height * 1.05:
        return "landscape"
    if height > width * 1.05:
        return "portrait"
    return "square"


def matches_orientation(photo, wanted):
    if wanted not in ("landscape", "portrait"):
        return True
    found = orientation(photo)
    return found is None or found == wanted or found == "square"


def _day_distance(a, b):
    """Distance in days between two month/day pairs, ignoring the year (wraps around new year)."""
    def as_date(d):
        day = min(d.day, 28) if d.month == 2 and d.day == 29 else d.day
        return date(2001, d.month, day)  # non-leap reference year
    delta = abs((as_date(a) - as_date(b)).days)
    return min(delta, 365 - delta)


def is_memory(photo, now, kind, window_days, tz):
    taken = photo_datetime(photo, tz)
    if taken is None:
        return False
    if kind == MEMORY_DAY:
        return taken.year < now.year and _day_distance(taken, now) <= window_days
    return taken.month == now.month  # MEMORY_MONTH: this month, every year (current one included)


def memory_pool_key(now, kind, window_days):
    """Changes when the set of memories changes (new month / new day)."""
    if kind == MEMORY_DAY:
        return f"day{now.month:02d}{now.day:02d}w{window_days}"
    return f"month{now.month:02d}"


# ------------------------------------------------------------------ weighted round-robin

def weighted_pick(weights, state):
    """Smooth weighted round-robin (as in nginx). weights: {key: weight>0}; state: {key: current}.

    Returns the chosen key; mutates state. Over N picks each key is chosen ~N*w/sum(w) times,
    spread evenly (e.g. weights 2:1 -> A A B A A B, never long runs).
    """
    weights = {k: w for k, w in weights.items() if w > 0}
    if not weights:
        return None
    total = sum(weights.values())
    for key in list(state):
        if key not in weights:
            del state[key]
    for key, weight in weights.items():
        state[key] = state.get(key, 0) + weight
    chosen = max(weights, key=lambda k: (state[k], k))
    state[chosen] -= total
    return chosen


def album_weights(album_keys, counts, shares, auto):
    """Target weight per album: its photo count (auto) or its configured percentage."""
    if not auto:
        manual = {k: max(0.0, float(shares.get(k) or 0)) for k in album_keys}
        if sum(manual.values()) > 0:
            return {k: w for k, w in manual.items() if counts.get(k)}
    return {k: float(counts.get(k, 0)) for k in album_keys if counts.get(k)}


def auto_shares(counts):
    """Percentages proportional to photo counts, rounded to sum to 100."""
    total = sum(counts.values())
    if not total:
        return {k: 0 for k in counts}
    raw = {k: 100 * c / total for k, c in counts.items()}
    shares = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - shares[k], reverse=True)[:100 - sum(shares.values())]:
        shares[k] += 1
    return shares


# ------------------------------------------------------------------ shuffle bags

def next_from_bag(bag, candidates, order="shuffle", last=None, predicate=None):
    """Pops the next photo id from bag (a list, mutated), refilling it from candidates when empty.

    candidates: list of photo dicts of the pool. order: shuffle | newest | oldest.
    predicate: optional filter for this pick only (e.g. same orientation for a pair);
    matching ids are taken out of the bag, others stay for later.
    """
    by_id = {p["id"]: p for p in candidates}
    bag[:] = [i for i in bag if i in by_id]
    if not any(predicate is None or predicate(by_id[i]) for i in bag):
        if predicate is not None and bag:
            # Nothing suitable left in this round: look in a fresh round without resetting the bag.
            fresh = [p for p in candidates if predicate(p) and p["id"] != last]
            return random.choice(fresh) if fresh else None
        bag[:] = _fill(candidates, order, last)
    for index, photo_id in enumerate(bag):
        if predicate is None or predicate(by_id[photo_id]):
            del bag[index]
            return by_id[photo_id]
    return None


def _fill(candidates, order, last):
    if order in ("newest", "oldest"):
        ordered = sorted(candidates, key=lambda p: (p.get("taken") or 0, p["id"]), reverse=(order == "newest"))
        return [p["id"] for p in ordered]
    ids = [p["id"] for p in candidates]
    random.shuffle(ids)
    if len(ids) > 1 and ids[0] == last:
        ids.append(ids.pop(0))
    return ids
