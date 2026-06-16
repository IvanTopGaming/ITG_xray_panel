package xray

import (
	"context"
	"net"
	"testing"

	"github.com/itg/metrics-agent/internal/xrayapi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

type fakeStats struct {
	xrayapi.UnimplementedStatsServiceServer
}

func (f *fakeStats) QueryStats(_ context.Context, _ *xrayapi.QueryStatsRequest) (*xrayapi.QueryStatsResponse, error) {
	return &xrayapi.QueryStatsResponse{Stat: []*xrayapi.Stat{
		{Name: "user>>>tg42>>>traffic>>>downlink", Value: 5000},
	}}, nil
}

func TestQueryStatsRoundTrip(t *testing.T) {
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	xrayapi.RegisterStatsServiceServer(srv, &fakeStats{})
	go srv.Serve(lis)
	defer srv.Stop()

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	resp, err := xrayapi.NewStatsServiceClient(conn).QueryStats(context.Background(), &xrayapi.QueryStatsRequest{Pattern: "user>>>"})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.GetStat()) != 1 || resp.GetStat()[0].GetValue() != 5000 {
		t.Fatalf("stats = %+v", resp.GetStat())
	}
}
