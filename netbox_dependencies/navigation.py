from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

# The menu is deliberately short: the graph is the main entry point. Custom maps are in the left panel of the graph ("Manage")
# and the outage simulation is a right click on a device in the graph (the page with a device picker is reachable
# via the "Impact" link on Live outages).
menu = PluginMenu(
    label='Dependencies',
    icon_class='mdi mdi-graph-outline',
    groups=(
        ('Dependencies', (
            PluginMenuItem(
                link='plugins:netbox_dependencies:graph',
                link_text='Dependency graph',
                permissions=['netbox_dependencies.view_dependency'],
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_dependencies:add_multiple',
                        title='Add',
                        icon_class='mdi mdi-plus-thick',
                        permissions=['netbox_dependencies.add_dependency'],
                    ),
                ),
            ),
            PluginMenuItem(
                link='plugins:netbox_dependencies:tree',
                link_text='Dependency tree',
                permissions=['netbox_dependencies.view_dependency'],
            ),
            PluginMenuItem(
                link='plugins:netbox_dependencies:dependency_list',
                link_text='All dependencies (list)',
                permissions=['netbox_dependencies.view_dependency'],
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_dependencies:dependency_bulk_import',
                        title='Import',
                        icon_class='mdi mdi-upload',
                        permissions=['netbox_dependencies.add_dependency'],
                    ),
                ),
            ),
            PluginMenuItem(
                link='plugins:netbox_dependencies:live',
                link_text='Live outages',
                permissions=['netbox_dependencies.view_dependency'],
            ),
            PluginMenuItem(
                link='plugins:netbox_dependencies:suppression_list',
                link_text='Suppressed devices',
                permissions=['netbox_dependencies.view_devicesuppression'],
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_dependencies:suppression_add',
                        title='Suppress',
                        icon_class='mdi mdi-plus-thick',
                        permissions=['netbox_dependencies.add_devicesuppression'],
                    ),
                ),
            ),
        )),
        ('Configuration', (
            PluginMenuItem(
                link='plugins:netbox_dependencies:settings',
                link_text='Notifications',
                permissions=['netbox_dependencies.view_notificationsettings'],
            ),
            PluginMenuItem(
                link='plugins:netbox_dependencies:poller',
                link_text='ICMP poller',
                permissions=['netbox_dependencies.view_pollersettings'],
            ),
        )),
    ),
)
