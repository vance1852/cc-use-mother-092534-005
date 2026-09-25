"""节日公共服务协作基础层的服务端基础包。"""

from .genealogy import GenealogyService
from .service import DomainService

__all__ = ["DomainService", "GenealogyService"]
