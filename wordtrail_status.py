"""Progress reporting for the Wordtrail sync job.
Writes data/<book id>/status.json and pushes it to GitHub so the app can show real progress."""
import json
import os
import subprocess
import time


class Status:
    def __init__(self, book_id, tracks, root='data', push=True, min_gap=20):
        self.dir = os.path.join(root, str(book_id))
        self.path = os.path.join(self.dir, 'status.json')
        self.tracks = max(1, int(tracks))
        self.push = push
        self.min_gap = min_gap
        self._last_push = 0.0
        self._last_key = None
        os.makedirs(self.dir, exist_ok=True)

    def update(self, stage, msg='', track=None, frac=0.0, force=False):
        if stage == 'done':
            pct = 1.0
        elif track:
            pct = min(0.99, max(0.0, (track - 1 + max(0.0, min(1.0, frac))) / self.tracks))
        else:
            pct = 0.0
        data = {'stage': stage, 'msg': msg, 'track': track, 'tracks': self.tracks,
                'pct': round(pct, 3), 'updated': int(time.time())}
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
        key = (stage, msg, int(pct * 33))
        now = time.time()
        if self.push and (force or (key != self._last_key and now - self._last_push >= self.min_gap)):
            self._last_key = key
            self._last_push = now
            self._publish(stage)

    def _publish(self, stage):
        def git(*a):
            return subprocess.run(['git', *a], capture_output=True, text=True)
        try:
            if not git('config', 'user.email').stdout.strip():
                git('config', 'user.email', 'wordtrail-bot@users.noreply.github.com')
                git('config', 'user.name', 'wordtrail-bot')
            git('add', self.path)
            if git('commit', '-m', f'status: {stage} [skip ci]').returncode == 0:
                for _ in range(3):
                    git('pull', '--rebase', '--autostash')
                    if git('push', 'origin', 'HEAD').returncode == 0:
                        break
        except Exception:
            pass  # progress reporting must never stop the real job
