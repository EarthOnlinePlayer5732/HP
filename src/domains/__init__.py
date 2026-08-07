import importlib
import pkgutil
from pathlib import Path
from .domain_base import DomainBase

_DOMAIN_REGISTRY = {}
DOMAIN_NAMES = sorted(
    module_info.name.removeprefix("domain_")
    for module_info in pkgutil.iter_modules([str(Path(__file__).parent)])
    if module_info.name.startswith("domain_")
    and module_info.name != "domain_base")


def _register_loaded_domains():
    for cls in DomainBase.__subclasses__():
        name = cls.__name__.replace("Domain", "").lower()
        _DOMAIN_REGISTRY[name] = cls


def get_domain(domain_name):
    if domain_name in DOMAIN_NAMES and domain_name not in _DOMAIN_REGISTRY:
        importlib.import_module(f".domain_{domain_name}", __package__)
        _register_loaded_domains()
    if domain_name not in _DOMAIN_REGISTRY:
        raise ValueError(f"Domain {domain_name} not supported. Available: {DOMAIN_NAMES}")
    return _DOMAIN_REGISTRY[domain_name]()
