from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import StudentCertificateViewSet, StudentDocumentViewSet, StudentReferencePhotoViewSet, StudentViewSet

router = DefaultRouter()
# NOTE: specific prefixes must be registered before "" — otherwise the
# StudentViewSet detail route (^(?P<pk>[^/.]+)/$) swallows them.
router.register("reference-photos", StudentReferencePhotoViewSet, basename="student-reference-photo")
router.register("certificates", StudentCertificateViewSet, basename="student-certificate")
router.register("", StudentViewSet, basename="student")
router.register("documents/list", StudentDocumentViewSet, basename="student-document")

urlpatterns = [path("", include(router.urls))]
