from app.models import Client, Inbound


def test_inbound_label_present_when_set(app, db):
    ib = Inbound(tag="ib-with-label", port=51000, stream_settings="{}", label="🇩🇪 Frankfurt")
    db.session.add(ib)
    db.session.flush()
    c = Client(id="cid1", email="tg1_ib-with-label", inbound_tag="ib-with-label")
    db.session.add(c)
    db.session.commit()

    d = c.to_dict()
    assert d["inbound_label"] == "🇩🇪 Frankfurt"


def test_inbound_label_falls_back_to_tag_when_label_null(app, db):
    ib = Inbound(tag="ib-nolabel", port=51001, stream_settings="{}", label=None)
    db.session.add(ib)
    db.session.flush()
    c = Client(id="cid2", email="tg1_ib-nolabel", inbound_tag="ib-nolabel")
    db.session.add(c)
    db.session.commit()

    d = c.to_dict()
    assert d["inbound_label"] == "ib-nolabel"


def test_inbound_label_falls_back_to_tag_when_label_empty(app, db):
    ib = Inbound(tag="ib-empty", port=51002, stream_settings="{}", label="")
    db.session.add(ib)
    db.session.flush()
    c = Client(id="cid3", email="tg1_ib-empty", inbound_tag="ib-empty")
    db.session.add(c)
    db.session.commit()

    d = c.to_dict()
    assert d["inbound_label"] == "ib-empty"


def test_inbound_label_falls_back_when_inbound_missing(app, db):

    c = Client(id="cid4", email="tg1_orphan", inbound_tag="orphan-tag")
    db.session.add(c)
    db.session.commit()

    d = c.to_dict()
    assert d["inbound_label"] == "orphan-tag"
