FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

LABEL org.opencontainers.image.source=https://github.com/MikeSiLVO/Pakt
LABEL org.opencontainers.image.description="Plex-Trakt sync tool"

RUN pip install --no-cache-dir pakt \
    && useradd --create-home --uid 1000 pakt \
    && mkdir -p /home/pakt/.config/pakt \
    && chown -R pakt:pakt /home/pakt \
    && chmod 755 /home/pakt

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV HOME=/home/pakt
VOLUME /home/pakt/.config/pakt
EXPOSE 7258

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["pakt", "serve", "--host", "0.0.0.0"]
