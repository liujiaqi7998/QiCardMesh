"""Views serving the OpenAPI contract and Swagger UI."""

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from .schemas import build_schema


@require_safe
def openapi_schema(request):
    return JsonResponse(build_schema(), json_dumps_params={"ensure_ascii": False})


@require_safe
def swagger_ui(request):
    return render(request, "qicardmesh/swagger_ui.html")
