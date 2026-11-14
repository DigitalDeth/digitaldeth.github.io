#!/usr/bin/env python3
"""Builds activity.json for the Latest activity section of the Strickland Productions homepage.

Run by .github/workflows/activity.yml on a schedule. Reads only what the section shows:
- Screening Room (Firestore, project watchlist-dc6d2): the five most recently watched movies: title, year,
  format, poster link, date watched. Read with a short-lived Google token (GOOGLE_TOKEN) from a read-only
  service account. Notes, synopses, TV shows and everything else stay private.
- Checkpoint (public site checkpoint-tracker-ms.web.app): last played games and recent achievements. A platform's
  data file is downloaded only when Checkpoint published a new one, to keep its free hosting allowance.

A source that fails keeps its previous data in the feed; the other sources still update. Standard library only.
Env: GOOGLE_TOKEN, SR_UID. Usage: python3 activity_feed.py <path to activity.json>
"""
import gzip, json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Chicago')
SR_PROJECT = 'watchlist-dc6d2'
CP = 'https://checkpoint-tracker-ms.web.app'
UA = {'User-Agent': 'strickland-productions-activity/1.0'}
KEEP_DAYS = 35


def get(url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def log(*a):
    print(*a, flush=True)


# ---------- Screening Room ----------
def fval(f):
    if not f:
        return None
    k, v = next(iter(f.items()))
    if k == 'integerValue':
        return int(v)
    if k == 'nullValue':
        return None
    return v


def fmt_of(platform):
    p = (platform or '').lower()
    if '4k' in p:
        return '4K'
    if 'blu' in p:
        return 'BLU-RAY'
    if 'dvd' in p:
        return 'DVD'
    return 'DIGITAL' if p else ''


def screening_room():
    tok, uid = os.environ.get('GOOGLE_TOKEN'), os.environ.get('SR_UID')
    if not tok or not uid:
        raise RuntimeError('GOOGLE_TOKEN or SR_UID missing')
    url = (f'https://firestore.googleapis.com/v1/projects/{SR_PROJECT}/databases/(default)/documents/'
           f'users/{uid}:runQuery')
    q = {'structuredQuery': {'from': [{'collectionId': 'movies'}],
                             'orderBy': [{'field': {'fieldPath': 'watchedOn'}, 'direction': 'DESCENDING'}],
                             'limit': 150}}
    raw = get(url, {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}, json.dumps(q).encode())
    rows = []
    for x in json.loads(raw):
        d = x.get('document')
        if not d:
            continue
        f = d['fields']
        if fval(f.get('watched')) is not True or fval(f.get('removed')) is True:
            continue
        if (fval(f.get('type')) or 'movie') != 'movie':
            continue
        on, title = fval(f.get('watchedOn')), fval(f.get('title'))
        if not on or not title:
            continue
        poster = fval(f.get('poster')) or ''
        rows.append({'title': title, 'year': fval(f.get('year')) or '', 'format': fmt_of(fval(f.get('platform'))),
                     'poster': poster if poster.startswith('https://') else '', 'watchedOn': on[:10],
                     '_added': int(fval(f.get('added')) or 0)})
    rows.sort(key=lambda r: (r['watchedOn'], r['_added']), reverse=True)
    out = rows[:5]
    for r in out:
        r.pop('_added')
    return out


# ---------- Checkpoint ----------
def steam_extract(d):
    ib = d['meta'].get('iconBase') or 'https://shared.fastly.steamstatic.com/community_assets/images/apps/'
    games, ach = [], []
    for g in d['games']:
        if g.get('lastPlayedTs'):
            img = g.get('capsule') or g.get('header') or \
                f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{g['appid']}/header.jpg"
            games.append({'name': g['name'], 'platform': 'STEAM', 'image': img, 'lastPlayed': g['lastPlayedTs'],
                          'unlocked': g.get('unlocked') or 0, 'total': g.get('total') or 0})
        for r in g.get('a') or []:
            if r[2] and r[3]:
                ach.append({'name': r[0], 'game': g['name'], 'platform': 'STEAM',
                            'icon': f"{ib}{g['appid']}/{r[4]}" if r[4] else '', 'at': r[3]})
    return games, ach


def console_extract(d, plat):
    pre = d['meta'].get('iconPrefix') or ''
    games, ach = [], []
    for g in d['games']:
        if plat == 'xbox':
            label = 'XBOX'
        else:
            label = ((g.get('platforms') or ['PS'])[0] or 'PS').upper()
        if g.get('lastPlayedTs'):
            games.append({'name': g['name'], 'platform': label, 'image': g.get('image') or '',
                          'lastPlayed': g['lastPlayedTs'], 'unlocked': g.get('unlocked') or 0,
                          'total': g.get('total') or 0})
        for r in g.get('a') or []:
            if r[2] and r[3]:
                icon = r[4] or ''
                if icon.startswith('~'):
                    icon = pre + icon[1:]
                ach.append({'name': r[0], 'game': g['name'], 'platform': label, 'icon': icon, 'at': r[3]})
    return games, ach


def checkpoint(prev):
    build = json.loads(get(f'{CP}/build.json?t={int(time.time())}'))
    out = {}
    for plat in ('steam', 'xbox', 'psn'):
        fname = (build.get('files') or {}).get(plat)
        old = prev.get(plat) or {}
        if fname and old.get('file') == fname:
            out[plat] = old
            log(f'checkpoint {plat}: unchanged ({fname})')
            continue
        try:
            d = json.loads(gzip.decompress(get(f'{CP}/d/{fname}', timeout=120)))
            games, ach = steam_extract(d) if plat == 'steam' else console_extract(d, plat)
            cutoff = time.time() - KEEP_DAYS * 86400
            games.sort(key=lambda g: -g['lastPlayed'])
            ach.sort(key=lambda a: -a['at'])
            out[plat] = {'file': fname, 'games': games[:5], 'recent': ach[:5],
                         'times': [a['at'] for a in ach if a['at'] >= cutoff]}
            log(f'checkpoint {plat}: read {fname}, {len(games)} played games, {len(ach)} unlocks')
        except Exception as e:  # keep what we had for this platform
            log(f'checkpoint {plat}: FAILED ({e}); keeping previous data')
            if old:
                out[plat] = old
    return out


def summarize(cp):
    games = sorted((g for p in cp.values() for g in p.get('games', [])), key=lambda g: -g['lastPlayed'])[:3]
    ach = sorted((a for p in cp.values() for a in p.get('recent', [])), key=lambda a: -a['at'])[:3]
    times = [t for p in cp.values() for t in p.get('times', [])]
    today = datetime.now(TZ).date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per = {d: 0 for d in days}
    month = 0
    for t in times:
        dd = datetime.fromtimestamp(t, TZ).date()
        if dd in per:
            per[dd] += 1
        if dd.year == today.year and dd.month == today.month:
            month += 1
    week = [{'day': d.isoformat(), 'count': per[d]} for d in days]
    return games, ach, week, month


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'activity.json'
    try:
        prev = json.load(open(path))
    except Exception:
        prev = {}
    feed = {'movies': prev.get('movies') or [], 'checkpoint': prev.get('checkpoint') or {}}
    status = {}
    try:
        feed['movies'] = screening_room()
        status['screeningRoom'] = 'ok'
        log(f"screening room: {len(feed['movies'])} movies")
    except Exception as e:
        status['screeningRoom'] = 'kept previous'
        log(f'screening room: FAILED ({e}); keeping previous movies')
    try:
        feed['checkpoint'] = checkpoint(feed['checkpoint'])
        status['checkpoint'] = 'ok'
    except Exception as e:
        status['checkpoint'] = 'kept previous'
        log(f'checkpoint: FAILED ({e}); keeping previous data')
    games, ach, week, month = summarize(feed['checkpoint'])
    new = {'movies': feed['movies'], 'games': games, 'achievements': ach, 'week': week, 'achievementsThisMonth': month,
           'checkpoint': feed['checkpoint']}
    # Only rewrite (and so only commit) when something on the page changed.
    same = {k: v for k, v in prev.items() if k not in ('generatedAt', 'status')} == new
    if same:
        log('no change; activity.json left as is')
    else:
        new = {'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'status': status, **new}
        with open(path, 'w') as fh:
            json.dump(new, fh, ensure_ascii=False, separators=(',', ':'))
        log(f'wrote {path} ({os.path.getsize(path)} bytes)')
    if status.get('screeningRoom') != 'ok' and status.get('checkpoint') != 'ok':
        sys.exit(1)


if __name__ == '__main__':
    main()
