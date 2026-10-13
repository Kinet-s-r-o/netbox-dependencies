from django.db import migrations, models


def clear_positions(apps, schema_editor):
    apps.get_model('netbox_dependencies', 'NodePosition').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_dependencies', '0003_nodeposition'),
    ]

    operations = [
        migrations.RunPython(clear_positions, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='nodeposition',
            name='netbox_dependencies_nodeposition_unique',
        ),
        migrations.RemoveField(
            model_name='nodeposition',
            name='device',
        ),
        migrations.AddField(
            model_name='nodeposition',
            name='node',
            field=models.CharField(default='', max_length=20),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name='nodeposition',
            constraint=models.UniqueConstraint(
                fields=('node', 'scope'), name='netbox_dependencies_nodeposition_unique'),
        ),
    ]
