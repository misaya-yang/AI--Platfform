from types import SimpleNamespace

from assistant_service.core.gateway.policy_engine import AssistantPolicyEngine
from assistant_service.core.gateway.request_router import AssistantRequestRouter


def test_memory_mode_off_is_a_hard_ceiling_for_memory_profile() -> None:
    router = AssistantRequestRouter(AssistantPolicyEngine(default_memory_mode="auto"))

    routed = router.route(SimpleNamespace(memory_mode=" OFF ", memory_profile="hybrid"))

    assert routed.memory_mode == "off"
    assert routed.memory_profile == "off"


def test_memory_mode_keeps_a_narrower_explicit_profile() -> None:
    router = AssistantRequestRouter(AssistantPolicyEngine(default_memory_mode="auto"))

    routed = router.route(SimpleNamespace(memory_mode="strict", memory_profile="basic"))

    assert routed.memory_mode == "strict"
    assert routed.memory_profile == "basic"
