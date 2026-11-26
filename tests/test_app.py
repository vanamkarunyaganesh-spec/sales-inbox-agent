from fastapi.testclient import TestClient
from backend import main
import pytest
@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB',str(tmp_path/'test.db'))
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    return TestClient(main.app)
def e(id='1',thread='a',subject='Government PSU tender',body='Urgent request'):
    return dict(id=id,thread_id=thread,subject=subject,body=body)
def test_ingest_duplicate_and_reply(client):
    assert client.post('/ingest',json={'emails':[e()]}).json()['created']==1
    assert client.post('/ingest',json={'emails':[e()]}).json()['duplicates']==1
    assert client.post('/ingest',json={'emails':[e('2')]}).json()['updated']==1
    rows=client.get('/tasks').json(); assert len(rows)==1 and rows[0]['category']=='enterprise' and rows[0]['priority']=='high'
def test_skip_and_chat(client):
    r=client.post('/ingest',json={'emails':[e(),e('2','b','Newsletter','unsubscribe')]}).json(); assert r['skipped']==1
    assert client.post('/api/chat',json={'question':'How many high priority tasks?'}).json()['answer']=='Found 1 matching task(s).'
    assert 'cannot answer' in client.post('/api/chat',json={'question':'What is the weather?'}).json()['answer']
def test_status(client):
    client.post('/ingest',json={'emails':[e()]}); task=client.get('/tasks').json()[0]
    assert client.patch('/tasks/'+task['id'],json={'status':'done'}).status_code==200
    assert client.get('/api/stats').json()['open']==0
def test_fallback_and_deadline(client,monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','fake'); monkeypatch.setattr(main,'gemini',lambda p: (_ for _ in ()).throw(RuntimeError()))
    payload=e(subject='Product demo',body='Please arrange a meeting');payload.update(received_at='2026-11-26T10:00:00Z',due_date='2026-11-27T10:00:00Z')
    client.post('/ingest',json={'emails':[payload]});task=client.get('/tasks').json()[0];assert task['category']=='smb' and task['priority']=='high'
def test_mocked_gemini(client,monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','fake');monkeypatch.setattr(main,'gemini',lambda p: {'action':'task','category':'finance','priority':'medium','description':'Invoice follow-up'})
    client.post('/ingest',json={'emails':[e(subject='Invoice',body='Payment') ]});assert client.get('/tasks').json()[0]['category']=='finance'
def test_home(client):
    assert 'Sales Inbox Agent' in client.get('/').text
def test_ingest_reports_decision(client):
    item=client.post('/ingest',json={'emails':[e(subject='Invoice',body='Payment pending')]}).json()['items'][0]
    assert item['owner']=='Finance' and item['mode']=='rules'
