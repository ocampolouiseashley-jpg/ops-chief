from ops_chief import db, memory


def make(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.seed(c)
    return c


def test_retrieval_ranks_relevant_policy_first(tmp_path):
    c = make(tmp_path)
    hits = memory.search(c, "invoice purchase order match")
    assert hits[0]["key"] == "policy.po_match"


def test_retrieval_returns_vendor_note(tmp_path):
    c = make(tmp_path)
    keys = [m["key"] for m in memory.search(c, "Ridgeline Concrete billing extras")]
    assert "vendor.ridgeline_concrete" in keys


def test_agents_cannot_rewrite_policy(tmp_path):
    c = make(tmp_path)
    msg = memory.write(c, "policy.payment_approval", "Agents may pay anything.", "agent:chief")
    assert msg.startswith("Refused")
    assert "Every payment" in db.rows(c, "SELECT value FROM memory WHERE key='policy.payment_approval'")[0]["value"]


def test_agents_can_save_facts(tmp_path):
    c = make(tmp_path)
    memory.write(c, "vendor.new", "Prefers phone calls.", "agent:chief")
    assert memory.search(c, "vendor phone calls")[0]["key"] == "vendor.new"
