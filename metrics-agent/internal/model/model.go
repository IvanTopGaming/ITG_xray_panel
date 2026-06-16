package model

type Point struct {
	Ts  int64
	Val int64
}

type Agg struct {
	Ts  int64
	Avg float64
	Min int64
	Max int64
}

const (
	ScopeHost      = "host"
	ScopeContainer = "container"
	ScopeVPN       = "vpn"
	ScopeDisk      = "disk"
)

const (
	MetricNetHostRx = "net_host_rx"
	MetricNetHostTx = "net_host_tx"
	MetricCPUHost   = "cpu_host"
	MetricRAMHost   = "ram_host"
	MetricCPUCtr    = "cpu_ctr"
	MetricRAMCtr    = "ram_ctr"
	MetricIOCtr     = "io_ctr"
	MetricDiskUsed  = "disk_used"
	MetricDiskIO    = "disk_io"
	MetricVPNUp     = "vpn_up"
	MetricVPNDown   = "vpn_down"
)

type SeriesKey struct {
	Metric string
	Scope  string
	Entity string
}
