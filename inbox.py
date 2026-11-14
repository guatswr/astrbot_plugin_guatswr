"""Single-process SQLite inbox. Receipt and snapshot checkpoints survive reloads."""

import json
import sqlite3
import time

from .protocol import canonical, digest


class Inbox:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS inbox (
            id TEXT PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
            error TEXT, created REAL NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS inbox_queue ON inbox(state,due,created)')
        # Exactly one worker per plugin; interrupted work can be replayed.
        self.db.execute("UPDATE inbox SET state='pending' WHERE state='working'")
        self.db.commit()

    def put(self, identifier, payload, revive=False):
        encoded, checksum = canonical(payload), digest(payload)
        row = self.db.execute('SELECT hash,state,checkpoint FROM inbox WHERE id=?', (identifier,)).fetchone()
        if row:
            if row['hash'] != checksum:
                raise ValueError('同一 delivery_id 的内容发生变化。')
            if row['state'] == 'failed' and revive:
                checkpoint = json.loads(row['checkpoint'])
                checkpoint.pop('failure_report', None)
                with self.db:
                    self.db.execute("UPDATE inbox SET state='pending',attempts=0,due=0,error=NULL,checkpoint=? WHERE id=?",
                                    (canonical(checkpoint), identifier))
            return False
        # Leave headroom against authenticated-but-misconfigured senders.
        if self.db.execute("SELECT count(*) FROM inbox WHERE state IN ('pending','working')").fetchone()[0] >= 10000:
            raise OverflowError('接收队列已满。')
        with self.db:
            self.db.execute('INSERT INTO inbox(id,hash,payload,created) VALUES(?,?,?,?)',
                            (identifier, checksum, encoded, time.time()))
        return True

    def take(self):
        row = self.db.execute("SELECT * FROM inbox WHERE state='pending' AND due<=? ORDER BY created,id LIMIT 1",
                              (time.time(),)).fetchone()
        if row is None:
            return None
        with self.db:
            self.db.execute("UPDATE inbox SET state='working',attempts=attempts+1 WHERE id=?", (row['id'],))
        return {'id': row['id'], 'payload': json.loads(row['payload']),
                'checkpoint': json.loads(row['checkpoint']), 'attempts': row['attempts'] + 1}

    def checkpoint(self, identifier, checkpoint):
        with self.db:
            self.db.execute('UPDATE inbox SET checkpoint=? WHERE id=?', (canonical(checkpoint), identifier))

    def done(self, identifier):
        with self.db:
            # Drop QQ snapshots and private message bodies after processing, keep
            # the digest/ID so later redelivery remains deduplicated.
            self.db.execute("UPDATE inbox SET state='done',payload='{}',checkpoint='{}',error=NULL WHERE id=?", (identifier,))

    def defer(self, identifier, due):
        with self.db:
            self.db.execute("UPDATE inbox SET state='pending',due=?,attempts=MAX(0,attempts-1),error='scheduled' WHERE id=?",
                            (due, identifier))

    def release_deferred(self, identifier):
        with self.db:
            return bool(self.db.execute(
                "UPDATE inbox SET due=0,error=NULL WHERE id=? AND state='pending' AND error='scheduled'",
                (identifier,)).rowcount)

    def fail(self, job, code, permanent=False):
        final = permanent or job['attempts'] >= 5
        with self.db:
            self.db.execute('UPDATE inbox SET state=?,due=?,error=? WHERE id=?',
                            ('failed' if final else 'pending', time.time() + min(300, 2 ** job['attempts']), code, job['id']))

    def summary(self):
        return {row[0]: row[1] for row in self.db.execute('SELECT state,count(*) FROM inbox GROUP BY state')}

    def failed(self, page=1):
        return [dict(row) for row in self.db.execute(
            "SELECT id,error,attempts FROM inbox WHERE state='failed' ORDER BY created DESC LIMIT 10 OFFSET ?",
            ((page - 1) * 10,))]

    def retry_notification(self, identifier):
        row = self.db.execute("SELECT payload FROM inbox WHERE id=? AND state='failed'", (identifier,)).fetchone()
        if row is None:
            raise ValueError('未找到本地失败任务，已完成任务不能重发。')
        payload = json.loads(row['payload'])
        if payload['event'] in ('members.profile_required', 'members.sync.request'):
            raise ValueError('私信请从网站重试投递；成员采集失败请从网站重新发起采集。')
        # Keep per-group checkpoints: a multi-group legacy notification must not
        # resend to groups which already acknowledged the earlier attempt.
        with self.db:
            self.db.execute("UPDATE inbox SET state='pending',attempts=0,due=0,error=NULL WHERE id=?", (identifier,))

    def close(self):
        self.db.close()
