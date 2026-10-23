from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("repositories", views.RepositoryViewSet)
router.register("decisions", views.DecisionViewSet)
router.register("changes", views.CodeChangeViewSet)
router.register("entities", views.CodeEntityViewSet)
router.register("incidents", views.IncidentViewSet)
router.register("root-causes", views.RootCauseViewSet)
router.register("implements-edges", views.ImplementsEdgeViewSet)
router.register("caused-by-edges", views.CausedByEdgeViewSet)
router.register("sessions", views.SessionViewSet)

urlpatterns = router.urls + [
    path("capture/pr-why/", views.PRWhyCaptureView.as_view(), name="capture-pr-why"),
    path("review-queue/counts/", views.ReviewCountsView.as_view(), name="review-counts"),
    path("review-queue/", views.ReviewQueueView.as_view(), name="review-queue"),
    path("settings/llm/", views.LLMSettingsView.as_view(), name="settings-llm"),
]
