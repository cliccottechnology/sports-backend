from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0005_attendancerecord_makeup_for'),
    ]

    operations = [
        migrations.AlterField(
            model_name='attendancephotoevidence',
            name='provider',
            field=models.CharField(default='faceauth', max_length=30),
        ),
    ]
