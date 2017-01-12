from django.db import transaction
from rest_framework import viewsets

from .serializers import TaskSerializer, CreateTaskSerializer
from .models import Task


class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer

    def get_serializer(self, data=None, *args, **kwargs):
        if getattr(self, 'creating', False):
            return CreateTaskSerializer(data=data)
        return super(TaskViewSet, self).get_serializer(data, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self.creating = True
        with transaction.atomic():
            return super(TaskViewSet, self).create(request, *args, **kwargs)
