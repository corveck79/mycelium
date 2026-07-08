FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 go build -o /spore-nfs .

FROM alpine:3.20
COPY --from=build /spore-nfs /spore-nfs
EXPOSE 2049
ENTRYPOINT ["/spore-nfs"]
