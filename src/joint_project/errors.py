"""联合研发项目管理服务向 API 和 CLI 暴露的稳定错误。"""


class JointProjectError(RuntimeError):
    code = "joint_project_error"
    status = 400


class NotFound(JointProjectError):
    code = "not_found"
    status = 404


class Conflict(JointProjectError):
    code = "conflict"
    status = 409


class Forbidden(JointProjectError):
    code = "forbidden"
    status = 403


class InvalidState(JointProjectError):
    code = "invalid_state"
    status = 409


class ValidationFailed(JointProjectError):
    code = "validation_failed"
    status = 422
