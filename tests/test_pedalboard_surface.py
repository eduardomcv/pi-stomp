import inspect

from modalapi.pedalboard import Pedalboard


def test_the_disk_loading_surface_is_gone():
    for name in ("hydrate", "get_pedalboard_info", "get_plugin_data", "_binding_range"):
        assert name not in dir(Pedalboard)
    assert "root_uri" not in inspect.signature(Pedalboard).parameters
    assert "hydrated" not in vars(Pedalboard("T", None))
