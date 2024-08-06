# https://eksctl.io/installation/#docker
FROM public.ecr.aws/eksctl/eksctl:v0.188.0 as eksctl
FROM docker.io/amazon/aws-cli:2.17.22 as awscli
# found image via: https://hub.docker.com/search?q=kubectl
# podman container run --entrypoint=sh --rm -it docker.io/rancher/kubectl:"$(curl -L -s https://dl.k8s.io/release/stable.txt)"
FROM docker.io/rancher/kubectl:v1.30.3 as kubectl
FROM docker.io/python:3.12
COPY --from=eksctl /usr/local/bin/aws-iam-authenticator /usr/local/bin/aws-iam-authenticator
COPY --from=eksctl /usr/local/bin/eksctl /usr/local/bin/eksctl
# found via podman inspect docker.io/rancher/kubectl:v1.30.3
COPY --from=kubectl /bin/kubectl /usr/local/bin/
# https://github.com/aws/aws-cli/blob/18d64caf588186c396dd58ff2346a8ca9657fdcb/docker/Dockerfile
COPY --from=awscli /usr/local/aws-cli/ /usr/local/aws-cli/
COPY --from=awscli /usr/local/bin/ /usr/local/bin/
RUN set -ex && apt-get update && \
    apt-get install -y direnv jq bash-completion && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install pipx && pipx ensurepath \
    && pipx install pipenv
# source <(kubectl completion bash)
RUN set -ex && ( \
    echo 'eval "$(direnv hook bash)"' \
    && echo 'source /etc/bash_completion' \
    && kubectl completion bash \
    && echo 'eval "$(register-python-argcomplete pipx)"' \
    && echo 'export PATH="$PATH:/root/.pulumi/bin"' \
    ) >> /root/.bashrc
WORKDIR /tmp/
RUN set -ex && curl -o helm.tgz -fsSL "https://get.helm.sh/helm-$(curl -fsSL 'https://api.github.com/repos/helm/helm/releases/latest' | jq -r '.tag_name')-linux-amd64.tar.gz" \
    && tar -xzvf helm.tgz linux-amd64/helm && mv -v linux-amd64/helm /usr/local/bin/ && rm -rf linux-amd64/ helm.tgz
RUN set -ex && curl -fsSL "$(curl -s https://api.github.com/repos/siderolabs/talos/releases/latest | jq -r '.assets[] | select(.name | test("talosctl-linux-amd64$")) | .browser_download_url')" -o /usr/local/bin/talosctl \
    && chmod +x /usr/local/bin/talosctl
RUN set -ex && curl -fsSL https://get.pulumi.com | sh
WORKDIR /