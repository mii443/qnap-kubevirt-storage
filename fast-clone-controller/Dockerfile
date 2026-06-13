FROM alpine:3.20

RUN apk add --no-cache ca-certificates curl openssh-client python3 sshpass
RUN addgroup -S -g 65532 controller && adduser -S -D -H -u 65532 -G controller controller

WORKDIR /app
COPY controller.py /app/controller.py

USER 65532:65532
ENTRYPOINT ["python3", "/app/controller.py"]
