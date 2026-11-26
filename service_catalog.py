"""Standard service names for the electronics-shop repair intake workflow.

The catalogue is a set of operator-friendly suggestions, not a restriction:
operators can still type a custom service name and the operations layer stores it.
"""
from __future__ import annotations


SERVICE_KIND_OPTIONS = (
    ("Hardware · Diagnostics / fault finding", "Repair"),
    ("Hardware · Laptop / desktop cleaning and thermal service", "Servicing"),
    ("Hardware · RAM upgrade / replacement", "Repair"),
    ("Hardware · SSD / HDD installation or replacement", "Repair"),
    ("Hardware · Screen / display replacement", "Repair"),
    ("Hardware · Keyboard replacement", "Repair"),
    ("Hardware · Battery replacement", "Repair"),
    ("Hardware · Charging port / DC jack repair", "Repair"),
    ("Hardware · Motherboard / power fault repair", "Repair"),
    ("Hardware · Fan / cooling system repair", "Repair"),
    ("Software · Operating system installation / reinstallation", "Servicing"),
    ("Software · Driver installation / update", "Servicing"),
    ("Software · Licensed application installation / setup", "Servicing"),
    ("Software · OS boot / crash troubleshooting", "Servicing"),
    ("Software · Virus / malware cleanup", "Servicing"),
    ("Software · System and application updates", "Servicing"),
    ("Software · Data backup / transfer", "Servicing"),
    ("Software · Data recovery assessment", "Servicing"),
    ("Software · Device setup / account configuration", "Servicing"),
    ("Phone · Screen replacement", "Repair"),
    ("Phone · Battery replacement", "Repair"),
    ("Phone · Charging port repair", "Repair"),
    ("Phone · Speaker / microphone / camera repair", "Repair"),
    ("Phone · Software update / reset", "Servicing"),
    ("Phone · Data transfer / new-phone setup", "Servicing"),
    ("Console · PlayStation cleaning / thermal service", "Servicing"),
    ("Console · PlayStation HDMI port repair", "Repair"),
    ("Console · PlayStation storage upgrade", "Repair"),
    ("Console · PlayStation system software troubleshooting", "Servicing"),
    ("Console · Controller troubleshooting / setup", "Servicing"),
    ("Networking · Router setup / configuration", "Servicing"),
    ("Networking · Wi-Fi troubleshooting", "Servicing"),
    ("Networking · LAN / switch configuration", "Servicing"),
    ("Networking · Network cabling / termination", "Servicing"),
    ("Networking · Network diagnostics", "Servicing"),
    ("General · Preventive maintenance", "Servicing"),
    ("General · Hardware servicing", "Servicing"),
    ("General · Software servicing", "Servicing"),
    ("General · General maintenance", "Servicing"),
    ("General · Other / custom service", "Servicing"),
)

SERVICE_OPTIONS = tuple(name for name, _kind in SERVICE_KIND_OPTIONS)
_SERVICE_KIND_BY_NAME = dict(SERVICE_KIND_OPTIONS)


def suggested_work_kind(service_name: str, default: str = "Repair") -> str:
    """Suggest Repair vs Servicing for a selected standard or typed service."""
    name = str(service_name or "").strip()
    if name in _SERVICE_KIND_BY_NAME:
        return _SERVICE_KIND_BY_NAME[name]
    lowered = name.casefold()
    service_terms = (
        "install", "setup", "configuration", "configure", "backup", "transfer",
        "recovery", "update", "maintenance", "servicing", "cleaning", "thermal",
        "network", "software", "virus", "malware", "troubleshooting", "trouble-shooting",
    )
    if any(term in lowered for term in service_terms):
        return "Servicing"
    return default if default in {"Repair", "Servicing"} else "Repair"
