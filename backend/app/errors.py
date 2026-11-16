class DomainError(Exception):
    status_code = 422


class InsufficientStockError(DomainError):
    status_code = 409


class ProductNotFoundError(DomainError):
    status_code = 404


class OrderNotFoundError(DomainError):
    status_code = 404


class InvalidOrderStatusError(DomainError):
    status_code = 422


class DuplicateOrderError(DomainError):
    status_code = 409


class InventoryConflictError(DomainError):
    status_code = 409


class UnauthorizedOperationError(DomainError):
    status_code = 403
