"""极地科考站协作基础服务的服务端基础包。"""

from .loan import LoanService
from .service import DomainService

__all__ = ["DomainService", "LoanService"]
