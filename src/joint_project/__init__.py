"""产学研联合研发项目管理服务。"""

from .contracts import AgreementDraft, ValidationError
from .service import ProjectService

__all__ = ["AgreementDraft", "ProjectService", "ValidationError"]
__version__ = "0.1.0"
