import { useEffect, useMemo } from 'react';
import { useSubInfo } from '@/hooks/useSubInfo';
import { pickLang, t } from '@/lib/i18n';
import ErrorState from '@/components/ErrorState';
import Loading from '@/components/Loading';
import Header from '@/components/Header';
import Hero from '@/components/Hero';
import Summary from '@/components/Summary';
import Nodes from '@/components/Nodes';
import Footer from '@/components/Footer';

export default function App() {
  const lang = useMemo(pickLang, []);
  const { data, error, loading, reload } = useSubInfo();

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  useEffect(() => {
    if (data) document.title = data.brand || t('default_brand', lang);
  }, [data, lang]);

  if (loading) return <Loading lang={lang} />;
  if (error || !data) return <ErrorState lang={lang} onRetry={reload} />;

  return (
    <div className="mx-auto max-w-3xl px-4 pb-16 pt-8">
      <Header data={data} lang={lang} />
      {data.nodes.length > 0 && <Hero data={data} lang={lang} />}
      <Summary data={data} lang={lang} />
      <Nodes data={data} lang={lang} />
      <Footer data={data} lang={lang} />
    </div>
  );
}
