from utilities.choices import ChoiceSet


class DependencyTypeChoices(ChoiceSet):
    key = 'Dependency.dependency_type'

    TYPE_NETWORK = 'network'
    TYPE_POWER = 'power'
    TYPE_OTHER = 'other'

    CHOICES = [
        (TYPE_NETWORK, 'Network', 'blue'),
        (TYPE_POWER, 'Power', 'orange'),
        (TYPE_OTHER, 'Other', 'gray'),
    ]


class MediumChoices(ChoiceSet):
    key = 'Dependency.medium'

    ETHERNET = 'ethernet'
    OPTICAL = 'optical'
    RADIO = 'radio'
    UNSPECIFIED = 'unspecified'

    CHOICES = [
        (ETHERNET, 'Ethernet', 'blue'),
        (OPTICAL, 'Optical', 'orange'),
        (RADIO, 'Radio', 'cyan'),
        (UNSPECIFIED, 'Unspecified', 'gray'),
    ]


class StateChoices(ChoiceSet):
    key = 'DeviceStatus.state'

    UP = 'up'
    DOWN = 'down'
    UNKNOWN = 'unknown'

    CHOICES = [
        (UP, 'Up', 'green'),
        (DOWN, 'Down', 'red'),
        (UNKNOWN, 'Unknown', 'gray'),
    ]


class SourceChoices(ChoiceSet):
    key = 'DeviceStatus.source'

    ZABBIX = 'zabbix'
    ICMP = 'icmp'

    CHOICES = [
        (ZABBIX, 'Zabbix', 'purple'),
        (ICMP, 'ICMP poller', 'cyan'),
    ]


class DestinationKindChoices(ChoiceSet):
    key = 'NotificationDestination.kind'

    TEAMS = 'teams'
    SLACK = 'slack'
    GENERIC = 'generic'

    CHOICES = [
        (TEAMS, 'Microsoft Teams (Workflow webhook)', 'blue'),
        (SLACK, 'Slack (incoming webhook)', 'green'),
        (GENERIC, 'Generic webhook (JSON)', 'gray'),
    ]
