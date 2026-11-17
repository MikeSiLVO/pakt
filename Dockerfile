FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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
