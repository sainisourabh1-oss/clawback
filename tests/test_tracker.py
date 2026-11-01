from datetime import date, timedelta
from pathlib import Path
from clawback.tracker import connect, set_status, update_dates, reminders, list_subscriptions, price_history, upsert_detection
from clawback.detect import Hit
from clawback.aggregate import aggregate
import pytest

def test_tracker_review_dates_history(tmp_path):
    p=tmp_path/'state.sqlite3'
    sub=aggregate([Hit('Example','example.com','Monthly plan','₹1,200 per year','receipt',.8,'subscription; renew')])[0]
    sid=upsert_detection(sub,p)
    assert list_subscriptions(p)[0]['status']=='suspected'
    set_status(sid,'confirmed',p)
    update_dates(sid,trial_end=(date.today()+timedelta(days=2)).isoformat(),renewal_date=(date.today()+timedelta(days=5)).isoformat(),notes='check price',path=p)
    assert len(reminders(7,p))==1
    assert price_history(sid,p)[0]['currency']=='INR'
    set_status(sid,'cancelled',p)
    assert reminders(7,p)==[]

def test_invalid_status_and_date(tmp_path):
    with pytest.raises(ValueError): set_status(999,'maybe',tmp_path/'db.sqlite')
    with pytest.raises(ValueError): update_dates(999,trial_end='tomorrow',path=tmp_path/'db.sqlite')


def test_tracker_keeps_distinct_plan_identities(tmp_path):
    p = tmp_path / 'multi.sqlite3'
    subs = aggregate([
        Hit('Example', 'example.com', 'Example Premium receipt', '$10.00 monthly', 'receipt', .8),
        Hit('Example', 'example.com', 'Example Business receipt', '$40.00 monthly', 'receipt', .8),
    ])
    ids = [upsert_detection(sub, p) for sub in subs]
    assert len(set(ids)) == 2
    assert len(list_subscriptions(p)) == 2


def test_migrates_legacy_database_preserving_related_rows(tmp_path):
    import sqlite3
    p = tmp_path / 'legacy.sqlite3'
    db = sqlite3.connect(p)
    db.executescript('''
      CREATE TABLE subscriptions (
        id INTEGER PRIMARY KEY, vendor TEXT NOT NULL, domain TEXT NOT NULL,
        amount REAL, currency TEXT NOT NULL DEFAULT '?', cycle TEXT NOT NULL DEFAULT 'unknown',
        status TEXT NOT NULL DEFAULT 'suspected', trial_end TEXT, renewal_date TEXT,
        notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(domain, currency));
      CREATE TABLE price_history (id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        amount REAL NOT NULL, currency TEXT NOT NULL, observed_on TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual');
      CREATE TABLE evidence (id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        subject TEXT NOT NULL, kind TEXT NOT NULL, confidence REAL NOT NULL, observed_at TEXT NOT NULL,
        UNIQUE(subscription_id, subject, kind));
      INSERT INTO subscriptions(id,vendor,domain,amount,currency,cycle,status,created_at,updated_at)
        VALUES(1,'Example','example.com',10,'USD','month','confirmed','now','now');
      INSERT INTO price_history(subscription_id,amount,currency,observed_on) VALUES(1,10,'USD','2026-01-01');
      INSERT INTO evidence(subscription_id,subject,kind,confidence,observed_at) VALUES(1,'Old invoice','receipt',0.8,'now');
    ''')
    db.commit(); db.close()
    db = connect(p)
    assert 'identity_key' in {row[1] for row in db.execute('PRAGMA table_info(subscriptions)')}
    assert db.execute('SELECT COUNT(*) FROM price_history').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
