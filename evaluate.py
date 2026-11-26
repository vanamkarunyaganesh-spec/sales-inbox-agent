"""Evaluate only deterministic fallback on a small synthetic labeled sample."""
import json
from pathlib import Path
from backend.main import Email, classify_rules
rows=json.loads((Path(__file__).parent/'data/sample_emails.json').read_text())
results=[]
for row in rows:
    d=classify_rules(Email.model_validate(row))
    prediction='skip' if d.action=='skip' else d.category
    results.append((row['id'],row['expected'],prediction))
correct=sum(a==b for _,a,b in results)
print(f'Offline synthetic classification: {correct}/{len(results)} ({correct/len(results):.1%})')
print('Category          Precision Recall')
for label in sorted({x for _,a,b in results for x in (a,b)}):
    tp=sum(a==b==label for _,a,b in results); fp=sum(b==label and a!=label for _,a,b in results); fn=sum(a==label and b!=label for _,a,b in results)
    print(f'{label:17} {tp/(tp+fp) if tp+fp else 0:9.2f} {tp/(tp+fn) if tp+fn else 0:6.2f}')
for i,a,b in results:
    if a!=b: print(f'Failure: {i}: expected {a}, predicted {b}')
