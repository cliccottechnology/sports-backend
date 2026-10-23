from django.db import models


class Document(models.Model):
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to="academy/%Y/%m/")
    file_type = models.CharField(max_length=20, blank=True)
    category = models.CharField(max_length=50, default="academy")  # academy/receipt/export/attachment
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    access_role = models.CharField(max_length=50, blank=True)  # optional role restriction
