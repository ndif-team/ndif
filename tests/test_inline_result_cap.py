"""The inline cap is a setting, and it reaches a model actor.

A blocking request's result up to ``NDIF_MAX_SOCKET_RESULT_BYTES`` goes back on
the COMPLETED response. The setting is read on the Ray head, and the Controller
hands each model actor the provider's ``to_env()``.
"""

from ndif.common.providers.objectstore import ObjectStoreProvider


def test_the_cap_is_read_from_the_env_and_carried_in_to_env(monkeypatch):
    # Restores the attribute from_env() rebinds below.
    monkeypatch.setattr(ObjectStoreProvider, "max_socket_result_bytes", None)

    monkeypatch.delenv("NDIF_MAX_SOCKET_RESULT_BYTES", raising=False)
    ObjectStoreProvider.from_env()
    assert ObjectStoreProvider.max_socket_result_bytes == 8 * 1024 * 1024

    monkeypatch.setenv("NDIF_MAX_SOCKET_RESULT_BYTES", "1048576")
    ObjectStoreProvider.from_env()
    assert ObjectStoreProvider.max_socket_result_bytes == 1048576
    assert ObjectStoreProvider.to_env()["NDIF_MAX_SOCKET_RESULT_BYTES"] == "1048576"
