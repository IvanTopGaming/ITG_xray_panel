package xray

import (
	"context"
	"time"

	"github.com/itg/metrics-agent/internal/xrayapi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type Client struct {
	addr string
}

func New(addr string) *Client { return &Client{addr: addr} }

func (c *Client) QueryStats(pattern string) ([]*xrayapi.Stat, error) {
	conn, err := grpc.NewClient(c.addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := xrayapi.NewStatsServiceClient(conn).QueryStats(ctx, &xrayapi.QueryStatsRequest{Pattern: pattern, Reset_: false})
	if err != nil {
		return nil, err
	}
	return resp.GetStat(), nil
}
