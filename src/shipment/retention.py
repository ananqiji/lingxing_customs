"""Shared date window for customs rows and pending shipment recovery."""
from datetime import date, timedelta


SHIPMENT_RETENTION_DAYS = 10


def shipment_retention_cutoff(today: date) -> date:
    """The earliest retained day, counting today as one calendar day."""
    return today - timedelta(days=SHIPMENT_RETENTION_DAYS - 1)
