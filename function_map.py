"""Map each company's department names to a small set of shared functions.

Every company names departments its own way ("SW Eng - Applications-674" at Okta,
"Engineering (Ads)" at Deliveroo), so comparing them needs a shared taxonomy.

Two layers, checked in this order:
1. Approved overrides (function_overrides.csv): exact company + department decisions,
   added through reviewed pull requests. They win over everything else.
2. Keyword rules (FUNCTION_RULES below): checked IN ORDER, first match wins, so order is
   a business rule. Security comes before Engineering so "Security Engineering" counts as
   Security; Finance comes before Sales so "Accounting Operations" isn't read as "Key Accounts".

Anything neither layer covers becomes "Other" with source "none", and the daily alerts
report it, so gaps are visible instead of hidden.
"""
import csv
import re
from pathlib import Path

OVERRIDES_PATH = Path(__file__).parent / "function_overrides.csv"

FUNCTION_RULES = [
    ("Security & Trust", r"secur|secops|\bsdl\b|\bgrc\b|trust|safeguard|surveillance|investigation|safety"),
    ("IT & Business Systems", r"^bt\b|business technology|tech ?ops|enterprise tech|collaboration suite|^it$"),
    ("People", r"people|talent|recruit|hrbp|total rewards|compensation|payroll|employee|leadership & org|^workplace$"),
    ("Legal, Policy & Compliance", r"legal|policy|polic(ies)?\b|compliance|regulat|audit|internal control|governance|"
                                   r"company secretary|global affairs|public benefit|kyc|due diligence|mlro|\bmlo\b|\brisk\b"),
    ("Research & ML", r"research|machine learning|\bml\b|robotics|life sciences"),
    ("Data & Analytics", r"data|analytic|insight"),
    ("Finance", r"financ|fp&a|accounting|\btax\b|treasury|controllership|\bcao\b|reporting"),
    ("Marketing & Comms", r"marketing|brand|communicat|comms|content|social|creative|media|demand gen|lifecycle|"
                          r"\bcrm\b|developer relations|devrel|campaign|engagement|event|education"),
    ("Product, Design & Programs", r"product|design|\bux|program|\btpm\b"),
    ("Engineering", r"\beng|tech|infra|compute|scaling|hardware|platform|software|^sw\b|devex|cloudmaker|"
                    r"applied ai|forward deployed|consumer devices|wallet|w3w|quality|digital workers|"
                    r"ai acceleration|for ai agents|clearing|labs"),
    ("Sales & GTM", r"sales|go to market|accounts?\b|commercial|business development|\bbd\b|partner|alliance|\bgsi\b|"
                    r"\bvip\b|growth|customer success|^se\b|public sector|for gov|deal strategy|smb|verticals|"
                    r"for work|\bdfw\b|restaurant|enablement|affiliate|regional teams"),
    ("Operations & Support", r"operation|\bops\b|support|service|\bline\b|^cs\b|site|delivery|case|administration|"
                             r"exec|strategy|sustainab|for good"),
]

_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in FUNCTION_RULES]

# Every valid function name. "Other" is allowed in overrides to record a reviewed decision
# that something fits no function (e.g. a talent-network page that isn't a real job).
ALLOWED_FUNCTIONS = [name for name, _ in FUNCTION_RULES] + ["Other"]


def clean_department(name):
    """Strip company-specific noise, such as Okta's trailing cost-center codes ('-674')."""
    if not name:
        return None
    return re.sub(r"-\d+$", "", name).strip()


def classify_function(department):
    """Keyword rules only. Returns a function name; 'Other' if no rule matches."""
    if not department:
        return "Other"
    for function, pattern in _COMPILED:
        if pattern.search(department):
            return function
    return "Other"


def load_overrides(path=OVERRIDES_PATH):
    """Read approved overrides as {(company, cleaned department): function}.

    Fails loudly on a bad row (unknown function, duplicate key) rather than silently
    mis-mapping jobs. The tests load this file too, so a bad row fails the pull request.
    """
    overrides = {}
    if not path.exists():
        return overrides
    with open(path, newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            key = (row["company"].strip(), clean_department(row["department"]))
            function = row["function"].strip()
            if function not in ALLOWED_FUNCTIONS:
                raise ValueError(f"{path.name} line {line}: unknown function {function!r}. "
                                 f"Use one of: {', '.join(ALLOWED_FUNCTIONS)}")
            if key in overrides:
                raise ValueError(f"{path.name} line {line}: duplicate override for {key}")
            overrides[key] = function
    return overrides


_OVERRIDES = load_overrides()


def map_function(company, department, overrides=None):
    """Return (function, source). Source is 'override', 'rule', or 'none' (unmapped)."""
    overrides = _OVERRIDES if overrides is None else overrides
    cleaned = clean_department(department)
    if (company, cleaned) in overrides:
        return overrides[(company, cleaned)], "override"
    function = classify_function(cleaned)
    return function, ("rule" if function != "Other" else "none")
