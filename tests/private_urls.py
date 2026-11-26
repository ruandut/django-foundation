from django.urls import path

from tests.views import private

urlpatterns = [path("", private)]
