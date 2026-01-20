FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/MikeSiLVO/Pakt
LABEL org.opencontainers.image.description="Plex-Trakt sync tool"

RUN pip install --no-cache-dir pakt

VOLUME /root/.config/pakt
EXPOSE 7258

CMD ["pakt", "serve", "--host", "0.0.0.0"]
