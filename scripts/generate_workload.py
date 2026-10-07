"""Generate the labelled simulation workload (Phase 6).

    python scripts/generate_workload.py                  # -> data/workload.jsonl (1,000 prompts)
    python scripts/generate_workload.py --seed 7 --out data/other.jsonl

Every prompt comes from a template with a GROUND-TRUTH tier (the tier a careful human would
pick), so routing can be scored. Tricky categories are included on purpose: hard tasks with no
complexity keywords, easy tasks that use "analyze"-type words, negation, simple code, JSON-output
requests, long inputs, multi-turn conversations, and simulated visible failures.

Train / test split is by TEMPLATE, not by prompt: two prompts from one template are
near-duplicates, so a per-prompt split would leak test data into anything tuned on the training
split. Roughly 30% of each category's templates are held out.

Deterministic: same seed -> byte-identical file.
"""
import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "workload.jsonl"

# ---------------------------------------------------------------- slot values
NAMES = ["Alice Martin", "Bob Chen", "Carla Diaz", "Deepak Rao", "Emma Novak", "Farid Haddad",
         "Grace Kim", "Hugo Laurent", "Ines Silva", "Jonas Berg"]
CITIES = ["Lisbon", "Pune", "Toronto", "Berlin", "Osaka", "Nairobi", "Austin", "Lyon"]
PRODUCTS = ["noise-cancelling headphones", "standing desk", "espresso machine", "trail shoes",
            "smart thermostat", "mechanical keyboard", "air purifier", "e-reader"]
SERVICES = ["checkout", "search", "billing", "notifications", "auth", "recommendations"]
LANGS = ["French", "German", "Spanish", "Japanese", "Portuguese", "Hindi"]
TOPICS = ["remote work policy", "Q3 sales results", "the new onboarding flow",
          "the data retention policy", "the pricing change", "the incident on Tuesday"]
SENTENCES = [
    "The customer reported that the order arrived two days late and the box was damaged.",
    "Revenue grew 12 percent quarter over quarter, driven mostly by the enterprise segment.",
    "The team agreed to move the launch to the second week of next month.",
    "Users can now export their reports as PDF from the settings page.",
    "Support tickets about login failures doubled after the last release.",
    "The vendor will deliver the replacement parts by Friday at the latest.",
    "We need to reduce cloud spend without slowing down the release cadence.",
    "The survey shows most users prefer shorter onboarding with optional tutorials.",
]


def paragraph(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(SENTENCES) for _ in range(n))


def fill(rng: random.Random, template: str) -> str:
    return template.format(
        name=rng.choice(NAMES), name2=rng.choice(NAMES), city=rng.choice(CITIES),
        product=rng.choice(PRODUCTS), service=rng.choice(SERVICES), lang=rng.choice(LANGS),
        topic=rng.choice(TOPICS), n=rng.randint(2, 9999), pct=rng.randint(3, 40),
        para=paragraph(rng, 3), long=paragraph(rng, 6), email=f"user{rng.randint(10, 999)}@example.com",
        price=f"{rng.randint(5, 900)}.{rng.randint(0, 99):02d}")


# ---------------------------------------------------------------- categories
# category -> (true tier, count, team, feature, max_tokens, tags, [templates])
# Feature names deliberately avoid those with seeded budget policies (summarize, chat-assistant):
# a FEATURE budget applies across all teams and would make results depend on unrelated traffic.
# A template is a user message, or a list of (role, content) turns for multi-turn prompts.
CATEGORIES = {
    "extraction": (1, 190, "ops", "extraction", 256, [], [
        "Extract the invoice number and total amount from this text: Invoice INV-{n} for {name}, total ${price}, due in 30 days.",
        "Pull out every email address from this note: please cc {email} and {name} on the thread.",
        "Extract the city and the date from: '{name} will visit our {city} office on day {n} of the project.'",
        "Get the order ID from this message: 'Hi, my order #{n} for a {product} never arrived.'",
        "List the person names mentioned here: {name} met {name2} in {city} to review the plan.",
        "Extract the price from: 'The {product} is on sale for ${price} until Sunday.'",
    ]),
    "formatting": (1, 120, "ops", "formatting", 256, [], [
        "Format this as a bulleted list: {name}, {name2}, {city}, {product}",
        "Fix typos in this sentence: 'Teh {product} arived yesterdy and wroks grate.'",
        "Convert this to title case: {para}",
        "Reformat this date into ISO 8601: the {n}th of March, 2026",
        "Uppercase the following product name: {product}",
        "Convert this list to CSV with columns name,city: {name} - {city}; {name2} - {city}",
    ]),
    "summarisation": (2, 110, "support", "summarisation", 512, [], [
        "Summarize this customer email in two sentences: {long}",
        "Give me the key points of these meeting notes: {long}",
        "Write a short summary of the discussion about {topic}: {long}",
        "Summarise this support ticket for the next agent: {long}",
        "Outline the main decisions from this update: {long}",
    ]),
    "classification": (2, 70, "support", "classify", 128, [], [
        "Classify the sentiment of this review as positive, neutral or negative: {para}",
        "Categorize this ticket into billing, technical or account: 'I was charged twice for my {product}.'",
        "Tag this message with one topic label: {para}",
        "Label this feedback as bug, feature request or praise: 'The {service} page is so slow lately.'",
    ]),
    "translation": (2, 50, "marketing", "translate", 512, [], [
        "Translate this into {lang}: {para}",
        "Translate the following product description into {lang}: Our {product} is light, durable and easy to set up.",
        "Please translate this support reply into {lang}: {para}",
        "Translate this headline into {lang}: 'Save {pct}% on every {product} this week'",
    ]),
    "reasoning": (3, 50, "engineering", "analysis", 1024, [], [
        "Analyze why conversion dropped {pct}% after we changed the {service} flow, and explain why each factor matters.",
        "Reason step by step: if churn is {pct}% monthly and we add 500 users a month, where does the user base stabilise?",
        "Evaluate the root cause of the latency regression in {service} given these symptoms: {para}",
        "Explain why our A/B test on the {service} page might be misleading and what we should change.",
    ]),
    "design": (3, 30, "engineering", "design", 1024, [], [
        "Design a rate limiter for the {service} API that is fair across tenants.",
        "Propose an architecture for moving {service} to an event-driven design, with trade-offs.",
        "Plan the migration of {service} from a monolith to services without downtime.",
        "Design a data model for multi-currency invoices with partial refunds.",
    ]),
    "debugging": (3, 40, "engineering", "code-review", 1024, [], [
        "Debug this function, it returns None for valid input:\n```python\ndef total(items):\n    for i in items:\n        s = s + i\n```",
        "Find the bug: this query is slow on large tables\n```sql\nSELECT * FROM orders WHERE lower(email) = '{email}'\n```",
        "Why does this crash with KeyError?\n```python\ncfg = {{}}\nprint(cfg['{service}'])\n```",
        "Debug the race condition in this code:\n```python\nif not cache.get(key):\n    cache.set(key, compute())\n```",
    ]),
    "comparison": (3, 30, "engineering", "analysis", 1024, [], [
        "Compare PostgreSQL and DynamoDB for the {service} workload and recommend one.",
        "Compare the trade-offs of monthly versus annual pricing for our {product} line.",
        "Compare Kafka and Redis Streams for {service} events, considering cost and operations.",
        "Critique these two approaches to caching {service} responses and pick one.",
    ]),
    "risky": (3, 60, "advisory", "advisory", 1024, [], [
        "Review this contract clause for liability risks: 'The supplier is not liable for any delay caused by {city} customs.'",
        "Is it safe to take ibuprofen with my current prescription? I am {n} years old.",
        "What dosage of paracetamol is appropriate for a child of {pct} kg?",
        "Should I move my retirement savings into a single tech stock? This is financial advice I will act on.",
        "Does our data processing in {city} comply with GDPR if we store emails for 10 years?",
        "Draft our response to the lawsuit threat from {name} about the {product} warranty.",
    ]),
    "hard_no_keywords": (3, 40, "engineering", "analysis", 1024, ["tricky"], [
        "Why would increasing the cache size make p99 latency worse for {service}?",
        "What happens to our margin if supplier prices rise {pct}% and we keep retail prices fixed?",
        "Our {service} error rate doubles every Monday morning. What could be going on?",
        "If we cut prices by {pct}% how many more {product} units must we sell to keep revenue flat?",
        "Two of our three datacenters agree and one disagrees. Which value should {service} trust?",
        "How would a 3x traffic spike affect a single-leader database behind {service}?",
    ]),
    "easy_heavy_words": (1, 40, "ops", "formatting", 128, ["tricky"], [
        "Analyze this list and just uppercase every name: {name}, {name2}",
        "Evaluate whether this string is empty: ''",
        "Design-wise, just convert '{product}' to lowercase.",
        "Plan: put these words in alphabetical order: {city}, {product}, {service}",
    ]),
    "negation": (1, 40, "ops", "extraction", 256, ["tricky"], [
        "Don't analyze anything, just list the dates in: we met on the 3rd, the 9th and the {n}th.",
        "No need to explain why, just extract the email from: contact {email} for details.",
        "Do not compare them, simply count the items: {name}, {name2}, {city}",
        "Without any analysis, copy the order number from: order #{n} shipped today.",
    ]),
    "code_simple": (1, 20, "engineering", "chat", 256, ["tricky"], [
        "What does this print?\n```python\nprint('{city}'.upper())\n```",
        "Rename the variable x to total in:\n```python\nx = {n}\n```",
        "Add a semicolon to the end of this line:\n```js\nconst city = '{city}'\n```",
    ]),
    "json_output": (1, 40, "ops", "extraction", 256, ["tricky"], [
        "Return JSON with keys name and email for: {name}, {email}",
        "Give me a JSON object with the fields product and price: {product} costs ${price}",
        "Output JSON: {{\"city\": ..., \"order\": ...}} from 'order {n} goes to {city}'",
        "Respond only with JSON listing these names: {name}, {name2}",
    ]),
    "long_input": (2, 10, "support", "summarisation", 512, ["tricky", "long"], [
        "Summarize the main complaints in this support log: {LONG}",
        "Give me the three key points of this transcript: {LONG}",
        "What are the action items in these notes? {LONG}",
    ]),
    "multi_turn": (2, 30, "support", "chat", 512, ["tricky", "multi_turn"], [
        [("user", "Can you analyze our churn numbers for {topic}?"),
         ("assistant", "Sure, please share the numbers."),
         ("user", "Actually, just summarize this note instead: {para}")],
        [("user", "Compare plan A and plan B for me."),
         ("assistant", "Plan A is cheaper; plan B is faster."),
         ("user", "Great. Now translate that into {lang}.")],
        [("system", "You are a helpful support assistant."),
         ("user", "My {product} stopped working."),
         ("assistant", "Sorry to hear that! What happens when you turn it on?"),
         ("user", "Please summarize my issue for the repair team: it beeps twice and the screen stays dark.")],
        [("user", "Hi!"),
         ("assistant", "Hello! How can I help?"),
         ("user", "Classify this feedback as positive or negative: {para}")],
    ]),
    "simulated_failure": (1, 30, "ops", "extraction", 256, ["simulated_failure"], [
        "Extract the order number from: order #{n} shipped [[mock:empty]]",
        "Fix typos in: 'teh {product} is grate' [[mock:refuse]]",
        "List the names in: {name} and {name2} [[mock:truncate]]",
        "Return JSON with the city from: 'shipping to {city}' [[mock:badjson]]",
    ]),
}
PRIORITIES = ["low", "normal", "high", "critical"]
PRIORITY_WEIGHTS = [0.15, 0.70, 0.12, 0.03]


def long_document(rng: random.Random) -> str:
    """~27,000 characters (~6,800 tokens): safely above the classifier's 6,000-token
    long-context threshold."""
    return paragraph(rng, 340)


def split_templates(category: str, count: int) -> set[int]:
    """Indices of held-out (test) templates: ~30% of each category, at least one,
    chosen deterministically from a hash of the category name."""
    n_test = max(1, round(count * 0.3))
    order = sorted(range(count), key=lambda i: hashlib.sha256(f"{category}:{i}".encode()).hexdigest())
    return set(order[:n_test])


def build_messages(rng: random.Random, template) -> list[dict]:
    if isinstance(template, list):
        return [{"role": role, "content": fill(rng, text)} for role, text in template]
    if "{LONG}" in template:
        return [{"role": "user", "content": template.replace("{LONG}", long_document(rng))}]
    return [{"role": "user", "content": fill(rng, template)}]


def generate(seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    records = []
    for category, (tier, count, team, feature, max_tokens, tags, templates) in CATEGORIES.items():
        held_out = split_templates(category, len(templates))
        for i in range(count):
            template_index = i % len(templates)          # every template used, evenly
            records.append({
                "category": category, "true_tier": tier,
                "template_id": f"{category}-{template_index}",
                "split": "test" if template_index in held_out else "train",
                "team_id": team, "feature": feature,
                "priority": rng.choices(PRIORITIES, PRIORITY_WEIGHTS)[0],
                "max_tokens": max_tokens, "tags": list(tags),
                "messages": build_messages(rng, templates[template_index]),
            })
    rng.shuffle(records)                                  # realistic interleaving
    for n, record in enumerate(records, start=1):
        record["id"] = f"w{n:04d}"
    return [{"id": r.pop("id"), **r} for r in records]


def distribution(records: list[dict]) -> str:
    lines = [f"{len(records)} prompts"]
    tiers = Counter(r["true_tier"] for r in records)
    lines.append("true tier: " + ", ".join(f"{t}: {tiers[t]} ({tiers[t] / len(records):.0%})"
                                           for t in sorted(tiers)))
    splits = Counter(r["split"] for r in records)
    lines.append(f"split: train {splits['train']}, test {splits['test']}")
    tricky = sum("tricky" in r["tags"] for r in records)
    failing = sum("simulated_failure" in r["tags"] for r in records)
    lines.append(f"tricky: {tricky}, simulated visible failures: {failing}")
    lines.append("categories: " + ", ".join(f"{c} {n}" for c, n in
                                            Counter(r["category"] for r in records).most_common()))
    lines.append("teams: " + ", ".join(f"{t} {n}" for t, n in
                                       Counter(r["team_id"] for r in records).most_common()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    records = generate(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()[:16]
    print(distribution(records))
    print(f"wrote {args.out} (sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
