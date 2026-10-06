# -*- coding: utf-8 -*-
"""验证：给歌库歌曲匹配网易云 id 并用外链实测可播 —— 决定"真播放"方案可行性"""
import json, urllib.request, urllib.parse, re, sys, time
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Referer': 'https://music.163.com/'}

def wy_search(kw, limit=6):
    u = 'https://music.163.com/api/search/get?s=%s&type=1&limit=%d&offset=0' % (urllib.parse.quote(kw), limit)
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=15).read().decode('utf-8', 'ignore')
        d = json.loads(r)
        out = []
        for x in (d.get('result') or {}).get('songs', []) or []:
            out.append({'id': x['id'], 'n': x['name'], 'a': (x.get('artists') or [{}])[0].get('name', '')})
        return out
    except Exception:
        return []

def playable(sid):
    u = 'https://music.163.com/song/media/outer/url?id=%s.mp3' % sid
    hh = dict(H); hh['Range'] = 'bytes=0-2047'
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=hh), timeout=20)
        b = r.read(2048)
        ct = (r.headers.get('Content-Type') or '')
        return (len(b) >= 1024 and 'audio' in ct), ct, len(b)
    except Exception as e:
        return False, str(e)[:40], 0

def norm(s):
    return re.sub(r'[^\u4e00-\u9fa5A-Za-z0-9]', '', (s or '')).lower()

def main():
    lib = json.load(open('data/library.json', encoding='utf-8'))
    songs = sorted(lib['songs'], key=lambda x: -(x.get('seen') or 0))[:24]
    hit = 0; exact = 0; rows = []
    for s in songs:
        cands = wy_search('%s %s' % (s['t'], s['s']))
        pick = None; kind = '-'
        for c in cands:
            if norm(c['n']) == norm(s['t']):
                pick = c
                kind = 'exact' if (norm(s['s']) and norm(s['s']) in norm(c['a'])) else 'exact-翻唱'
                break
        if not pick and cands:
            pick = cands[0]; kind = '近似'
        if pick:
            ok, ct, n = playable(pick['id'])
            if ok:
                hit += 1
                if kind.startswith('exact') and '翻唱' not in kind: exact += 1
                rows.append((s['t'], s['s'], pick['id'], kind, ct, n))
                print('  OK  %-16s %-10s id=%-10s %-10s %s %dB' % (s['t'][:16], s['s'][:10], pick['id'], kind, ct, n))
            else:
                print('  --  %-16s %-10s id=%-10s %-10s %s' % (s['t'][:16], s['s'][:10], pick['id'], kind, ct))
        else:
            print('  XX  %-16s %-10s 无候选' % (s['t'][:16], s['s'][:10]))
        time.sleep(0.25)
    print('\n可播命中 %d/%d = %.0f%%  其中原唱精确匹配 %d' % (hit, len(songs), 100.0*hit/len(songs), exact))

main()
