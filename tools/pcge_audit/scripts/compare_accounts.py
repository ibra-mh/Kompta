import json
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

with (DATA_DIR / "existing_accounts.json").open("r", encoding="utf-8") as f:
    existing = json.load(f)

with (DATA_DIR / "official_plan_cgnc.json").open("r", encoding="utf-8") as f:
    official = json.load(f)

def norm(s):
    if not s:
        return ""
    # normalize spaces, quotes, lower case, accents
    s = s.lower().strip()
    s = re.sub(r"[\'’\"«»\.,\(\)\-\–]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

existing_by_code = {}
for acc in existing:
    code = acc["code"]
    if code not in existing_by_code:
        existing_by_code[code] = []
    existing_by_code[code].append(acc)

already_present = []
missing = []
needs_review = []

for off in official:
    code = off["code"]
    off_label = off["label"]
    page = off["printed_page"]
    classe = off["classe"]

    if code in existing_by_code:
        ex_list = existing_by_code[code]
        # check if labels match closely
        ex_labels = [e["label"] for e in ex_list]
        matched_label = False
        for el in ex_labels:
            if norm(el) == norm(off_label):
                matched_label = True
                break
        
        if matched_label:
            already_present.append({
                "code": code,
                "official_label": off_label,
                "kompta_label": ex_labels[0],
                "printed_page": page,
                "classe": classe
            })
        else:
            # Different label or slight variant
            needs_review.append({
                "code": code,
                "official_label": off_label,
                "kompta_label": " / ".join(ex_labels),
                "printed_page": page,
                "classe": classe,
                "reason": "Libellé différent ou variante d'intitulé"
            })
    else:
        missing.append({
            "code": code,
            "official_label": off_label,
            "printed_page": page,
            "classe": classe
        })

print("Comparison Summary:")
print(f"- Already present (exact/matching label): {len(already_present)}")
print(f"- Needs review (code exists but label difference): {len(needs_review)}")
print(f"- Missing (not in Kompta): {len(missing)}")
print(f"Total official accounts: {len(official)}")

with (DATA_DIR / "comparison_result.json").open("w", encoding="utf-8") as f:
    json.dump({
        "already_present": already_present,
        "needs_review": needs_review,
        "missing": missing
    }, f, ensure_ascii=False, indent=2)

