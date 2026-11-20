"""Map each company's department names to a small set of shared functions.

Every company names departments its own way ("SW Eng - Applications-674" at Okta,
"Engineering (Ads)" at Deliveroo), so comparing them needs a shared taxonomy.

Rules are checked IN ORDER and the first match wins, so order is a business rule.
For example, Security comes before Engineering so "Security Engineering" counts as
Security, and Finance comes before Sales so "Accounting Operations" isn't read as
"Key Accounts". Anything unmatched becomes "Other" and is reported, not hidden.
"""
import re

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


def clean_department(name):
    """Strip company-specific noise, such as Okta's trailing cost-center codes ('-674')."""
    if not name:
        return None
    return re.sub(r"-\d+$", "", name).strip()


def classify_function(department):
    if not department:
        return "Other"
    for function, pattern in _COMPILED:
        if pattern.search(department):
            return function
    return "Other"
