"""Import feature ORM models here as they are added for Alembic discovery."""

from src.assignments.models import ShiftAssignment
from src.availability.models import AvailabilityEntry, AvailabilitySubmission
from src.core.database import Base
from src.positions.models import Position
from src.schedules.models import Schedule, ShiftRequirement
from src.users.models import Role, User

__all__ = [
    "Base",
    "Role",
    "User",
    "Position",
    "Schedule",
    "ShiftRequirement",
    "AvailabilitySubmission",
    "AvailabilityEntry",
    "ShiftAssignment",
]
