"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class LicenseConflict(DomainError):
    """展出沿谱系计算时存在未满足的许可，阻断登记并指明确切来源。"""

    code = "license_conflict"
    status = 422

    def __init__(self, message: str, blockers: list) -> None:
        super().__init__(message)
        self.blockers = blockers
