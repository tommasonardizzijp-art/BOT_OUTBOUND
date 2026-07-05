from app.utils.device_pool import DEVICE_POOL, device_for_account, _HARDWARE_KEYS


def test_every_device_has_all_hardware_keys():
    for d in DEVICE_POOL:
        for k in _HARDWARE_KEYS:
            assert k in d, f"device {d.get('model')} manca la chiave {k}"


def test_stable_per_username():
    # Stesso username -> sempre lo stesso device (un telefono che non cambia = realistico).
    a = device_for_account("primeroa_adv7")
    b = device_for_account("primeroa_adv7")
    assert a == b


def test_distinct_usernames_spread():
    # Username diversi non devono cadere tutti sullo stesso device.
    got = {device_for_account(f"user_{i}")["model"] for i in range(30)}
    assert len(got) >= 5


def test_real_cluster_accounts_get_distinct_devices():
    # I 4 account reali del cluster devono ricevere telefoni DIVERSI (rompe la firma).
    users = [
        "primero_azienda_cbd", "primeroa_adv7",
        "antonino.o.o.54", "claudio.abbigliamentovincente",
    ]
    models = [device_for_account(u)["model"] for u in users]
    assert len(set(models)) == len(models), f"collisione device sul cluster: {models}"


def test_returns_copy_not_shared_reference():
    # Deve tornare una copia: mutarla non deve corrompere il pool.
    d = device_for_account("tizio")
    d["model"] = "HACKED"
    assert device_for_account("tizio")["model"] != "HACKED"
