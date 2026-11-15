"""Errors raised by the delivery builder."""


class DeliveryError(RuntimeError):
    """A problem that makes the delivery unsafe or incomplete.

    Messages always identify the source file and object path whenever those
    are known, so that a failed build is diagnosable without the original
    tool context.
    """


def where(file_path: str | None, object_path: str | None) -> str:
    bits = []
    if file_path:
        bits.append(f"file {file_path}")
    if object_path:
        bits.append(f"object {object_path}")
    return " (" + ", ".join(bits) + ")" if bits else ""
