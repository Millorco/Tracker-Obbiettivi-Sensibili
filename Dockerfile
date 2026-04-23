FROM python:3.11-slim

# Install vsftpd and supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
    vsftpd \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Python deps
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app/ .

# FTP directories
RUN mkdir -p /ftp-data/uploads /app/db \
    && useradd -m -s /bin/bash ftpuser \
    && chown -R ftpuser:ftpuser /ftp-data

# vsftpd config
COPY vsftpd.conf /etc/vsftpd.conf
RUN mkdir -p /var/run/vsftpd/empty

# supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000 21 21100-21110

ENTRYPOINT ["/entrypoint.sh"]
