from utilities.choices import ChoiceSet


class ServerModeChoices(ChoiceSet):
    """How the kea-sync daemon treats a Server.

    cb/agent are SYNC TARGETS: the daemon converges them to what NetBox
    defines (a zero-prefix sync target converges to empty!). observe is
    lease reflection only and is structurally never written to — the
    right mode for instances whose config is owned elsewhere (e.g. an
    OPNsense-managed firewall).
    """

    MODE_CB = "cb"
    MODE_AGENT = "agent"
    MODE_OBSERVE = "observe"

    CHOICES = [
        (MODE_CB, "Config backend (DB writer + host_cmds reservations)"),
        (MODE_AGENT, "Control agent (whole-config sync)"),
        (MODE_OBSERVE, "Observe only (lease reflection)"),
    ]
