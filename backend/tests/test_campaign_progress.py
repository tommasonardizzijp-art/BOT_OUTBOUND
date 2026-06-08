"""_enrich_campaign deve esporre list_progress e bio_progress dai conteggi follower."""
from app.api.campaigns import compute_phase_progress
from app.models.follower import FollowerStatus


def test_list_progress_counts_all_followers():
    counts = {FollowerStatus.pending: 300, FollowerStatus.bio_scraped: 200}
    lp, bp = compute_phase_progress(counts, list_target=600, bio_target=400)
    assert lp == {"done": 500, "target": 600}
    # bio done = bio_scraped (+ stati a valle), pending+bio = 500
    assert bp == {"done": 200, "target": 400}


def test_progress_targets_none():
    counts = {FollowerStatus.pending: 50}
    lp, bp = compute_phase_progress(counts, list_target=None, bio_target=None)
    assert lp == {"done": 50, "target": None}
    assert bp == {"done": 0, "target": None}
