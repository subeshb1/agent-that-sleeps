# Built by the Lambda MicroVMs image builder on top of the AL2023 base image.
# The snapshot is taken after the server starts, so every MicroVM launched
# from this image skips all of this initialization.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y python3.12 python3.12-pip && dnf clean all

WORKDIR /app
COPY agent_server.py .

RUN python3.12 -m pip install --no-cache-dir "strands-agents>=1.15.0"

EXPOSE 8080
CMD ["python3.12", "agent_server.py"]
