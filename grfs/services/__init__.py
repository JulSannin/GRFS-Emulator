"""The RMC services.

Each module here answers one protocol. They are listed in `all_services` rather than discovered,
so that what the server answers is a visible list in one place.
"""


def all_services() -> list:
    from .auth import TicketGranting
    from .secure import SecureConnection
    return [TicketGranting, SecureConnection]
