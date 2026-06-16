package store

func (s *Store) Prune(rollup1mBefore, procBefore int64) error {
	if _, err := s.db.Exec(`DELETE FROM rollup_1m WHERE minute_ts < ?`, rollup1mBefore); err != nil {
		return err
	}
	if _, err := s.db.Exec(`DELETE FROM proc_sample WHERE ts < ?`, procBefore); err != nil {
		return err
	}
	return nil
}
